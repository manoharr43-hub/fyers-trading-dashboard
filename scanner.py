# ════════════════════════════════════════════════════════════════════════
# NSE AI PRO V13 — REFACTORED (FIXED: st.set_page_config removed)
# ════════════════════════════════════════════════════════════════════════
# FIXED: Removed st.set_page_config() — call it in app.py instead
# This version is compatible with multi-page Streamlit apps
# ════════════════════════════════════════════════════════════════════════

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

# ─── Core configuration ─────────────────────────────────────────────────
DATE_FROM = (datetime.today() - timedelta(days=365)).strftime("%Y-%m-%d")
DATE_TO = datetime.today().strftime("%Y-%m-%d")
FYERS_NSE_CM_SYMBOL_MASTER = "https://public.fyers.in/sym_details/NSE_CM.csv"
NIFTY_BENCHMARK_SYMBOL = "NSE:NIFTY50-INDEX"
MAX_WORKERS = 8
BATCH_SIZE = 50
BATCH_PAUSE_SECONDS = 1.0

# ─── High-volume filter configuration ───────────────────────────────────
MIN_RVOL_FOR_SIGNALS = 1.5  # Minimum relative volume (1.5x avg)
MIN_RVOL_FOR_LIVE_OB = 1.8  # Higher threshold for live OB signals

# ─── 15-min CISD scanner configuration ──────────────────────────────────
INTRADAY_CISD_RESOLUTION = "15"
INTRADAY_CISD_LOOKBACK_DAYS = 5
INTRADAY_MIN_RVOL = 1.5

# ─── Live OB signals configuration ──────────────────────────────────────
LIVE_OB_RESOLUTION = "15"
LIVE_OB_RESOLUTION_MINUTES = 15
LIVE_OB_LOOKBACK_DAYS = 5
LIVE_OB_AUTO_REFRESH_SECONDS = 180

# ─── Signal persistence ────────────────────────────────────────────────
SIGNALS_DIR = "signals"
SIGNALS_BUY_DIR = os.path.join(SIGNALS_DIR, "buy")
SIGNALS_SELL_DIR = os.path.join(SIGNALS_DIR, "sell")
CHARTS_DIR = "charts"
EXPORTS_DIR = "exports"
_SEEN_SIGNALS_FILE = os.path.join(SIGNALS_DIR, "_seen_signal_keys.json")
_SEEN_SIGNALS_MAX_KEEP = 5000
_LIVE_OB_MASTER_CSV = os.path.join(EXPORTS_DIR, "live_ob_signals.csv")

LOGS_DIR = "logs"
_VALID_EQ_SYMBOL_RE = re.compile(r"^NSE:[A-Z0-9&\-]+-EQ$")

try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
except Exception:
    from datetime import timezone
    IST = timezone(timedelta(hours=5, minutes=30))

# ─── Logger setup ──────────────────────────────────────────────────────
def _ensure_app_folders() -> None:
    """Create all required application folders."""
    for folder in (SIGNALS_DIR, SIGNALS_BUY_DIR, SIGNALS_SELL_DIR, LOGS_DIR, CHARTS_DIR, EXPORTS_DIR):
        os.makedirs(folder, exist_ok=True)

_ensure_app_folders()

logger = logging.getLogger("nse_scanner")
logger.setLevel(logging.INFO)
if not logger.handlers:
    try:
        _file_handler = logging.FileHandler(os.path.join(LOGS_DIR, "scanner.log"), encoding="utf-8")
        _file_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
        logger.addHandler(_file_handler)
    except OSError:
        logger.addHandler(logging.StreamHandler())

# ════════════════════════════════════════════════════════════════════════
# UTILITY FUNCTIONS
# ════════════════════════════════════════════════════════════════════════

def _now_ist() -> datetime:
    """Return current time in IST."""
    return datetime.now(IST)

def _format_signal_timestamp(ts, is_daily: bool = False) -> Tuple[str, str]:
    """Format timestamp to IST date/time strings."""
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    ts_ist = ts.tz_convert(IST)
    return ts_ist.strftime("%d-%b-%Y"), ts_ist.strftime("%H:%M:%S") + " IST"

def _validate_symbols(symbols) -> List[str]:
    """De-duplicate and validate NSE equity symbols."""
    seen = set()
    valid = []
    for s in symbols:
        if not isinstance(s, str):
            continue
        s = s.strip().upper()
        if not s or s in seen or not _VALID_EQ_SYMBOL_RE.match(s):
            continue
        seen.add(s)
        valid.append(s)
    return valid

# ════════════════════════════════════════════════════════════════════════
# SYMBOL LOADING
# ════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=60 * 60 * 12)
def load_nse_equity_symbols() -> List[str]:
    """Load NSE equity symbols from Fyers master list (cached 12h)."""
    try:
        resp = requests.get(FYERS_NSE_CM_SYMBOL_MASTER, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        st.error(f"Could not download Fyers symbol master: {e}")
        return []
    
    lines = [ln for ln in resp.text.strip().split("\n") if ln.strip()]
    if not lines:
        return []
    
    # Auto-detect symbol column
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
    
    if best_col is None:
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

# ════════════════════════════════════════════════════════════════════════
# CORE INDICATORS
# ════════════════════════════════════════════════════════════════════════

def calculate_rsi(close, period: int = 14):
    """Wilder's RSI."""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return (100 - (100 / (1 + rs))).fillna(50)

def calculate_macd(close):
    """Standard 12/26/9 MACD."""
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    return macd_line, signal_line, macd_line - signal_line

def calculate_atr(df, period: int = 14):
    """Average True Range."""
    h, l, c = df["High"], df["Low"], df["Close"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

def calculate_vwap_approx(df, window: int = 20):
    """Approximate VWAP over trailing candles."""
    d = df.tail(window)
    typical = (d["High"] + d["Low"] + d["Close"]) / 3
    vol_sum = d["Volume"].sum()
    if vol_sum <= 0:
        return round(float(d["Close"].iloc[-1]), 2)
    return round(float((typical * d["Volume"]).sum() / vol_sum), 2)

# ════════════════════════════════════════════════════════════════════════
# SAFE API CALLS WITH RETRY
# ════════════════════════════════════════════════════════════════════════

_HISTORY_MAX_RETRIES = 3
_HISTORY_BASE_DELAY_SECONDS = 1.0

def _safe_history(fyers, params: dict, max_retries: int = _HISTORY_MAX_RETRIES, 
                   base_delay: float = _HISTORY_BASE_DELAY_SECONDS):
    """Resilient Fyers API call with retry logic."""
    symbol = params.get("symbol", "UNKNOWN")
    last_err = "unknown error"
    
    for attempt in range(1, max_retries + 1):
        try:
            resp = fyers.history(params)
        except requests.exceptions.Timeout:
            last_err = "timeout"
        except requests.exceptions.ConnectionError:
            last_err = "network error"
        except Exception as e:
            last_err = f"error: {e}"
        else:
            if isinstance(resp, dict) and resp.get("s") == "ok":
                candles = resp.get("candles")
                if isinstance(candles, list):
                    return resp, None
            last_err = f"invalid response: {resp.get('message', 'unknown')}"
        
        if attempt < max_retries:
            time.sleep(base_delay * attempt)
    
    return None, f"{symbol}: {last_err} (after {max_retries} attempts)"

# ════════════════════════════════════════════════════════════════════════
# SCAN STATISTICS
# ════════════════════════════════════════════════════════════════════════

class ScanStats:
    """Track progress counters for a scan run."""
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

def _display_scan_summary(stats: ScanStats) -> None:
    """Show scan summary metrics."""
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Total Stocks", stats.total)
    c2.metric("Scanned", stats.scanned)
    c3.metric("Successful", stats.successful)
    c4.metric("Skipped", stats.skipped)
    c5.metric("Failed", stats.failed)
    c6.metric("Scan Time", f"{stats.elapsed_seconds:.1f}s")

# ════════════════════════════════════════════════════════════════════════
# HIGH-VOLUME SIGNAL FILTERING
# ════════════════════════════════════════════════════════════════════════

def _format_rvol_display(rvol_raw: float) -> str:
    """Format RVOL with emoji indicators."""
    display = f"{rvol_raw:.2f}x"
    if rvol_raw >= 3.0:
        display += " 🔥🔥"
    elif rvol_raw >= 2.0:
        display += " ❤️‍🔥"
    elif rvol_raw >= MIN_RVOL_FOR_SIGNALS:
        display += " 📈"
    return display

def _passes_volume_filter(rvol_raw: float, min_threshold: float = MIN_RVOL_FOR_SIGNALS) -> bool:
    """Check if signal passes high-volume filter."""
    return rvol_raw >= min_threshold

# ════════════════════════════════════════════════════════════════════════
# CORE SIGNAL GENERATION (FROM DAILY CANDLES)
# ════════════════════════════════════════════════════════════════════════

def _calculate_smc_and_cisd(df):
    """Smart Money Concepts / CISD detection (simplified)."""
    if len(df) < 30:
        return "Range ➖", "None", None

    close = df["Close"]
    
    # Simple BOS/CHOCH detection
    recent_high = df["High"].tail(30).max()
    recent_low = df["Low"].tail(30).min()
    last_close = float(close.iloc[-1])
    
    vol_avg = df["Volume"].tail(20).mean()
    last_volume = float(df["Volume"].iloc[-1])
    volume_confirmed = bool(vol_avg > 0 and last_volume > vol_avg)
    
    smc_structure = "Range ➖"
    cisd_signal = "None"
    event_ts = None
    
    # Detect breaks on volume
    if last_close > recent_high and volume_confirmed:
        smc_structure = "BOS 📈"
        cisd_signal = "Bullish CISD 🚀"
        event_ts = df["Time"].iloc[-1]
    elif last_close < recent_low and volume_confirmed:
        smc_structure = "BOS 📉"
        cisd_signal = "Bearish CISD 🩸"
        event_ts = df["Time"].iloc[-1]
    
    return smc_structure, cisd_signal, event_ts

# ════════════════════════════════════════════════════════════════════════
# 15-MIN CISD SCANNER FOR NSE STOCKS (HIGH-VOLUME ONLY)
# ════════════════════════════════════════════════════════════════════════

def _fetch_intraday_cisd_signal(fyers, symbol, resolution, timeframe_label):
    """Fetch 15-min CISD signal for NSE stock with volume filter."""
    stock_ticker = symbol.replace("NSE:", "").replace("-EQ", "")
    
    if not isinstance(symbol, str) or not _VALID_EQ_SYMBOL_RE.match(symbol):
        return None, f"{symbol}: invalid format"
    
    date_from = (datetime.today() - timedelta(days=INTRADAY_CISD_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    date_to = datetime.today().strftime("%Y-%m-%d")
    
    resp, err = _safe_history(fyers, {
        "symbol": symbol, "resolution": resolution, "date_format": "1",
        "range_from": date_from, "range_to": date_to, "cont_flag": "1",
    })
    
    if err or not resp:
        return None, err or f"{symbol}: no data"
    
    candles = resp.get("candles")
    if not candles or len(candles) < 30:
        return None, None  # Skip silently
    
    try:
        df = pd.DataFrame(candles, columns=["Time", "Open", "High", "Low", "Close", "Volume"])
        df["Time"] = pd.to_datetime(df["Time"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
        df[["Open", "High", "Low", "Close", "Volume"]] = df[["Open", "High", "Low", "Close", "Volume"]].apply(
            pd.to_numeric, errors="coerce")
        df = df.dropna(subset=["Open", "High", "Low", "Close"]).sort_values("Time").reset_index(drop=True)
        
        if len(df) < 30:
            return None, None
        
        smc_structure, cisd_signal, event_ts = _calculate_smc_and_cisd(df)
        
        if cisd_signal == "None":
            return None, None
        
        # HIGH-VOLUME FILTER
        vol_avg20 = df["Volume"].tail(20).mean()
        last_volume = float(df["Volume"].iloc[-1])
        rvol_raw = round(last_volume / vol_avg20, 2) if vol_avg20 > 0 else 0.0
        
        if not _passes_volume_filter(rvol_raw, INTRADAY_MIN_RVOL):
            return None, None  # Skip low-volume signals
        
        last_close = float(df["Close"].iloc[-1])
        atr = float(calculate_atr(df).iloc[-1])
        if pd.isna(atr) or atr <= 0:
            atr = last_close * 0.005
        
        is_up = "Bullish" in cisd_signal
        entry = round(last_close, 2)
        sl = round(entry - 1.0 * atr, 2) if is_up else round(entry + 1.0 * atr, 2)
        target = round(entry + 2.0 * atr, 2) if is_up else round(entry - 2.0 * atr, 2)
        
        risk = abs(entry - sl)
        reward = abs(target - entry)
        rr_ratio = round(reward / risk, 2) if risk > 0 else 0.0
        
        rsi_val = round(float(calculate_rsi(df["Close"]).iloc[-1]), 1)
        ai_score = round(min(max(50 + (rvol_raw * 10) + (10 if is_up else -10) + (rsi_val - 50) * 0.3, 0), 100), 1)
        confidence = round(min(95.0, max(35.0, 55 + min(rvol_raw, 3) * 8 + rr_ratio * 3)), 1)
        
        signal_date_str, signal_time_str = _format_signal_timestamp(event_ts if event_ts else df["Time"].iloc[-1])
        
        return {
            "Signal Date": signal_date_str,
            "Signal Time": signal_time_str,
            "Timeframe": timeframe_label,
            "Stock": stock_ticker,
            "Signal": "🟢 ▲ CISD UP" if is_up else "🔴 ▼ CISD DOWN",
            "Entry": entry,
            "Stoploss": sl,
            "Target": target,
            "RVOL": _format_rvol_display(rvol_raw),
            "Confidence %": confidence,
            "AI Score": ai_score,
            "RSI": rsi_val,
            "R:R": rr_ratio,
        }, None
    
    except Exception as e:
        return None, f"{symbol}: error ({type(e).__name__})"

def run_intraday_cisd_scan(fyers, symbols, resolution, timeframe_label):
    """Threaded scan for 15-min CISD signals (HIGH-VOLUME only)."""
    symbols = _validate_symbols(symbols)
    results, errors = [], []
    stats = ScanStats(total=len(symbols))
    progress = st.progress(0.0, text=f"Scanning {timeframe_label} CISD 0 / {len(symbols)}")
    done = 0
    
    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i:i + BATCH_SIZE]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(_fetch_intraday_cisd_signal, fyers, s, resolution, timeframe_label): s 
                      for s in batch}
            for future in as_completed(futures):
                try:
                    res, err = future.result()
                except Exception as e:
                    res, err = None, f"{futures[future]}: error ({type(e).__name__})"
                
                if res:
                    results.append(res)
                if err:
                    errors.append(err)
                
                stats.record(has_result=bool(res), has_error=bool(err))
                done += 1
                progress.progress(done / len(symbols), text=f"Scanning {timeframe_label} CISD {done} / {len(symbols)}")
        
        if i + BATCH_SIZE < len(symbols):
            time.sleep(BATCH_PAUSE_SECONDS)
    
    progress.empty()
    gc.collect()
    return results, errors, stats

# ════════════════════════════════════════════════════════════════════════
# LIVE OB SIGNALS WITH HIGH-VOLUME FILTER
# ════════════════════════════════════════════════════════════════════════

def _load_seen_signal_keys() -> set:
    """Load persisted seen signal keys."""
    try:
        with open(_SEEN_SIGNALS_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()

def _save_seen_signal_keys(keys: set) -> None:
    """Persist seen signal keys."""
    try:
        trimmed = sorted(keys)[-_SEEN_SIGNALS_MAX_KEEP:]
        with open(_SEEN_SIGNALS_FILE, "w", encoding="utf-8") as f:
            json.dump(trimmed, f)
    except OSError as e:
        logger.warning(f"Could not persist signal keys: {e}")

def _fetch_live_ob_signal(fyers, symbol, seen_keys):
    """Fetch 15-min Live OB signal with HIGH-VOLUME filter."""
    stock_ticker = symbol.replace("NSE:", "").replace("-EQ", "")
    
    if not isinstance(symbol, str) or not _VALID_EQ_SYMBOL_RE.match(symbol):
        return None, None
    
    date_from = (datetime.today() - timedelta(days=LIVE_OB_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    date_to = datetime.today().strftime("%Y-%m-%d")
    
    resp, err = _safe_history(fyers, {
        "symbol": symbol, "resolution": LIVE_OB_RESOLUTION, "date_format": "1",
        "range_from": date_from, "range_to": date_to, "cont_flag": "1",
    })
    
    if err or not resp:
        return None, None
    
    candles = resp.get("candles")
    if not candles or len(candles) < 31:
        return None, None
    
    try:
        df = pd.DataFrame(candles, columns=["Time", "Open", "High", "Low", "Close", "Volume"])
        df["Time"] = pd.to_datetime(df["Time"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
        df[["Open", "High", "Low", "Close", "Volume"]] = df[["Open", "High", "Low", "Close", "Volume"]].apply(
            pd.to_numeric, errors="coerce")
        df = df.dropna(subset=["Open", "High", "Low", "Close"]).sort_values("Time").reset_index(drop=True)
        
        if len(df) < 30:
            return None, None
        
        smc_structure, cisd_signal, event_ts = _calculate_smc_and_cisd(df)
        
        if cisd_signal == "None":
            return None, None
        
        # HIGH-VOLUME FILTER (stricter for live signals)
        vol_avg = df["Volume"].tail(20).mean()
        last_volume = float(df["Volume"].iloc[-1])
        rvol_raw = round(last_volume / vol_avg, 2) if vol_avg > 0 else 0.0
        
        if not _passes_volume_filter(rvol_raw, MIN_RVOL_FOR_LIVE_OB):
            return None, None  # Skip low-volume signals
        
        direction = "BUY" if "Bullish" in cisd_signal else "SELL"
        is_buy = direction == "BUY"
        
        signal_date_str, signal_time_str = _format_signal_timestamp(event_ts if event_ts else df["Time"].iloc[-1])
        dedup_key = f"{symbol}|{LIVE_OB_RESOLUTION}|{signal_date_str}|{signal_time_str}|{direction}"
        
        if dedup_key in seen_keys:
            return None, None  # Already notified
        
        last_close = float(df["Close"].iloc[-1])
        atr = float(calculate_atr(df).iloc[-1])
        if pd.isna(atr) or atr <= 0:
            atr = last_close * 0.005
        
        entry = round(last_close, 2)
        if is_buy:
            sl = round(entry - 1.0 * atr, 2)
            risk = entry - sl
            target1 = round(entry + 1.5 * risk, 2)
            target2 = round(entry + 3.0 * risk, 2)
        else:
            sl = round(entry + 1.0 * atr, 2)
            risk = sl - entry
            target1 = round(entry - 1.5 * risk, 2)
            target2 = round(entry - 3.0 * risk, 2)
        
        rr_ratio = round(abs(target1 - entry) / risk, 2) if risk > 0 else 0.0
        
        rsi_val = round(float(calculate_rsi(df["Close"]).iloc[-1]), 1)
        macd_line, macd_sig, _ = calculate_macd(df["Close"])
        macd_bullish = bool(macd_line.iloc[-1] > macd_sig.iloc[-1])
        
        signal_strength = "🟢 Strong" if rvol_raw >= 2.5 else ("🟡 Medium" if rvol_raw >= MIN_RVOL_FOR_LIVE_OB else "🔴 Weak")
        
        return {
            "dedup_key": dedup_key,
            "Signal Date": signal_date_str,
            "Signal Time": signal_time_str,
            "Stock": stock_ticker,
            "Direction": direction,
            "Signal": "🟢 BUY" if is_buy else "🔴 SELL",
            "LTP": entry,
            "Entry": entry,
            "Stop Loss": sl,
            "Target 1": target1,
            "Target 2": target2,
            "Risk:Reward": rr_ratio,
            "RVOL": _format_rvol_display(rvol_raw),
            "RSI": rsi_val,
            "MACD": "🟢 Bullish" if macd_bullish else "🔴 Bearish",
            "Signal Strength": signal_strength,
            "Structure": smc_structure,
        }, None
    
    except Exception as e:
        logger.exception(f"Error processing {symbol}")
        return None, None

def run_live_ob_signal_scan(fyers, symbols, seen_keys):
    """Threaded scan for live 15-min OB signals (HIGH-VOLUME only)."""
    symbols = _validate_symbols(symbols)
    all_rows, new_rows, errors = [], [], []
    stats = ScanStats(total=len(symbols))
    updated_keys = set(seen_keys)
    
    progress = st.progress(0.0, text=f"Scanning Live OB 0 / {len(symbols)}")
    done = 0
    
    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i:i + BATCH_SIZE]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(_fetch_live_ob_signal, fyers, s, seen_keys): s for s in batch}
            for future in as_completed(futures):
                try:
                    res, err = future.result()
                except Exception as e:
                    res, err = None, f"{futures[future]}: error ({type(e).__name__})"
                
                if res:
                    all_rows.append(res)
                    if res["dedup_key"] not in updated_keys:
                        updated_keys.add(res["dedup_key"])
                        new_rows.append(res)
                
                if err:
                    errors.append(err)
                
                stats.record(has_result=bool(res), has_error=bool(err))
                done += 1
                progress.progress(done / max(len(symbols), 1), text=f"Scanning Live OB {done} / {len(symbols)}")
        
        if i + BATCH_SIZE < len(symbols):
            time.sleep(BATCH_PAUSE_SECONDS)
    
    progress.empty()
    _save_seen_signal_keys(updated_keys)
    gc.collect()
    
    return all_rows, new_rows, errors, stats, updated_keys

# ════════════════════════════════════════════════════════════════════════
# EXPORT UTILITIES
# ════════════════════════════════════════════════════════════════════════

def to_excel_bytes(df, sheet_name: str = "Signals") -> bytes:
    """Export DataFrame to Excel."""
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name=sheet_name[:31])
    buf.seek(0)
    return buf.getvalue()

def to_csv_bytes(df) -> bytes:
    """Export DataFrame to CSV."""
    return df.to_csv(index=False).encode("utf-8")

def to_json_bytes(df) -> bytes:
    """Export DataFrame to JSON."""
    return df.to_json(orient="records", indent=2, force_ascii=False).encode("utf-8")

def _style_dataframe(df):
    """Apply basic styling."""
    try:
        styler = df.style
        return styler
    except Exception:
        return df

# ════════════════════════════════════════════════════════════════════════
# MAIN SCANNER FUNCTION (NO st.set_page_config here!)
# ════════════════════════════════════════════════════════════════════════

def show_scanner(fyers) -> None:
    """Main scanner UI with 3 core tabs.
    
    NOTE: st.set_page_config() should be called in app.py BEFORE calling this function!
    """
    st.title("🚀 NSE Scanner — High-Volume Live Signals")
    st.caption(f"🕒 {_now_ist().strftime('%d-%b-%Y %H:%M:%S')} IST")
    
    symbols = load_nse_equity_symbols()
    if not symbols:
        st.error("No symbols loaded. Check network access.")
        return
    
    st.info(f"✅ Loaded {len(symbols)} NSE equity stocks  |  "
            f"🔥 High-volume filter active (RVOL ≥ {MIN_RVOL_FOR_SIGNALS}x for daily, "
            f"≥ {MIN_RVOL_FOR_LIVE_OB}x for live OB)")
    
    col1, col2 = st.columns([2, 1])
    with col1:
        limit = st.number_input("Scan limit (0 = all)", min_value=0, max_value=len(symbols), 
                               value=200, step=50)
    with col2:
        auto_refresh = st.checkbox("🔁 Auto-refresh every 180s", value=False)
    
    scan_universe = symbols if limit == 0 else symbols[:limit]
    
    # ─── TAB 1: 15-MIN CISD SIGNALS ────────────────────────────────────
    # ─── TAB 2: LIVE OB SIGNALS ────────────────────────────────────────
    
    tab1, tab2 = st.tabs(["⚡ 15-Min CISD (High-Volume)", "🔔 Live OB Signals (15-Min)"])
    
    with tab1:
        st.markdown("**15-Minute CISD Signals** — Volume-confirmed BOS breaks on high RVOL ≥ 1.5x  \n"
                   "Only stocks with relative volume above threshold are shown.")
        
        if st.button("⚡ Run 15-Min CISD Scan", key="cisd_run"):
            with st.spinner("Scanning 15-min candles..."):
                cisd_results, cisd_errors, cisd_stats = run_intraday_cisd_scan(
                    fyers, scan_universe, INTRADAY_CISD_RESOLUTION, "15 Minutes"
                )
                st.session_state["cisd_df"] = pd.DataFrame(cisd_results)
                st.session_state["cisd_errors"] = cisd_errors
                st.session_state["cisd_stats"] = cisd_stats
        
        if "cisd_stats" in st.session_state:
            _display_scan_summary(st.session_state["cisd_stats"])
        
        cisd_df = st.session_state.get("cisd_df")
        if cisd_df is not None and not cisd_df.empty:
            cisd_sorted = cisd_df.sort_values("Confidence %", ascending=False)
            st.dataframe(_style_dataframe(cisd_sorted), use_container_width=True, height=400)
            
            # Export options
            col_xl, col_csv, col_json = st.columns(3)
            with col_xl:
                st.download_button("📥 Excel", data=to_excel_bytes(cisd_sorted, "15-Min CISD"),
                                  file_name=f"cisd_15m_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx",
                                  mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                  key="dl_cisd_xlsx")
            with col_csv:
                st.download_button("📥 CSV", data=to_csv_bytes(cisd_sorted),
                                  file_name=f"cisd_15m_{_now_ist().strftime('%Y%m%d_%H%M')}.csv",
                                  mime="text/csv", key="dl_cisd_csv")
            with col_json:
                st.download_button("📥 JSON", data=to_json_bytes(cisd_sorted),
                                  file_name=f"cisd_15m_{_now_ist().strftime('%Y%m%d_%H%M')}.json",
                                  mime="application/json", key="dl_cisd_json")
        else:
            st.info("Run scan above to see 15-min CISD signals.")
        
        if st.session_state.get("cisd_errors"):
            with st.expander(f"⚠️ Errors ({len(st.session_state['cisd_errors'])})"):
                st.text("\n".join(st.session_state["cisd_errors"][:10]))
    
    with tab2:
        st.markdown("**Live OB Signals** — Real-time 15-min order block BUY/SELL  \n"
                   "Only high-volume signals (RVOL ≥ 1.8x) are shown. Automatically deduplicates.")
        
        lob_col1, lob_col2 = st.columns([2, 1])
        with lob_col1:
            st.caption(f"Monitoring {len(scan_universe)} stocks for live signals.")
        with lob_col2:
            lob_auto = st.checkbox("🔄 Auto-scan every 180s", value=False, key="lob_auto_refresh")
        
        run_lob = st.button("🔔 Scan Live OB Signals", key="live_ob_run")
        
        if run_lob or lob_auto:
            seen_keys = _load_seen_signal_keys()
            with st.spinner("Fetching live 15-min order blocks..."):
                lob_rows, lob_new, lob_errors, lob_stats, updated_keys = run_live_ob_signal_scan(
                    fyers, scan_universe, seen_keys
                )
                lob_df = pd.DataFrame([{k: v for k, v in r.items() if k != "dedup_key"} for r in lob_rows])
            
            st.session_state["lob_df"] = lob_df
            st.session_state["lob_errors"] = lob_errors
            st.session_state["lob_stats"] = lob_stats
            st.session_state["lob_last_run"] = _now_ist().strftime("%d-%b-%Y %H:%M:%S")
            
            # Show NEW signals as alerts
            for new_row in lob_new:
                msg = f"{new_row['Signal']} {new_row['Stock']} @ ₹{new_row['Entry']} "
                msg += f"(SL ₹{new_row['Stop Loss']}, T1 ₹{new_row['Target 1']}, "
                msg += f"T2 ₹{new_row['Target 2']}, RR {new_row['Risk:Reward']})"
                if new_row["Direction"] == "BUY":
                    st.success(f"🟢 NEW BUY: {msg}")
                else:
                    st.error(f"🔴 NEW SELL: {msg}")
        
        if "lob_stats" in st.session_state:
            _display_scan_summary(st.session_state["lob_stats"])
        
        if st.session_state.get("lob_last_run"):
            st.caption(f"Last scanned: {st.session_state['lob_last_run']} IST")
        
        lob_df = st.session_state.get("lob_df")
        if lob_df is not None and not lob_df.empty:
            buy_df = lob_df[lob_df["Direction"] == "BUY"]
            sell_df = lob_df[lob_df["Direction"] == "SELL"]
            
            st.markdown(f"### 🟢 Buy Signals ({len(buy_df)})")
            if not buy_df.empty:
                st.dataframe(_style_dataframe(buy_df.sort_values("RVOL", ascending=False)),
                           use_container_width=True, height=250)
            else:
                st.caption("None")
            
            st.markdown(f"### 🔴 Sell Signals ({len(sell_df)})")
            if not sell_df.empty:
                st.dataframe(_style_dataframe(sell_df.sort_values("RVOL", ascending=False)),
                           use_container_width=True, height=250)
            else:
                st.caption("None")
            
            # Export all
            col_xl, col_csv, col_json = st.columns(3)
            with col_xl:
                st.download_button("📥 Excel", data=to_excel_bytes(lob_df, "Live OB"),
                                  file_name=f"live_ob_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx",
                                  mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                  key="dl_lob_xlsx")
            with col_csv:
                st.download_button("📥 CSV", data=to_csv_bytes(lob_df),
                                  file_name=f"live_ob_{_now_ist().strftime('%Y%m%d_%H%M')}.csv",
                                  mime="text/csv", key="dl_lob_csv")
            with col_json:
                st.download_button("📥 JSON", data=to_json_bytes(lob_df),
                                  file_name=f"live_ob_{_now_ist().strftime('%Y%m%d_%H%M')}.json",
                                  mime="application/json", key="dl_lob_json")
        else:
            st.info("Run scan above or enable auto-refresh.")
        
        if st.session_state.get("lob_errors"):
            with st.expander(f"⚠️ Errors ({len(st.session_state['lob_errors'])})"):
                st.text("\n".join(st.session_state["lob_errors"][:10]))
        
        if lob_auto:
            time.sleep(LIVE_OB_AUTO_REFRESH_SECONDS)
            st.rerun()
