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
from ai_analysis_engine import analyze_market

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
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
    import xgboost as xgb
    XGBOOST_AVAILABLE = True
except ImportError:
    XGBOOST_AVAILABLE = False

DATE_FROM = (datetime.today() - timedelta(days=365)).strftime("%Y-%m-%d")
DATE_TO = datetime.today().strftime("%Y-%m-%d")
FYERS_NSE_CM_SYMBOL_MASTER = "https://public.fyers.in/sym_details/NSE_CM.csv"
NIFTY_BENCHMARK_SYMBOL = "NSE:NIFTY50-INDEX"
MAX_WORKERS = 8
BATCH_SIZE = 50
BATCH_PAUSE_SECONDS = 1.0
XGB_MODEL_PATH = "xgb_trend_model.json"
INTRADAY_CISD_LOOKBACK_DAYS = 5
SIGNALS_DIR = "signals"
SIGNALS_BUY_DIR = os.path.join(SIGNALS_DIR, "buy")
SIGNALS_SELL_DIR = os.path.join(SIGNALS_DIR, "sell")
LOGS_DIR = "logs"
CHARTS_DIR = "charts"
EXPORTS_DIR = "exports"
_SEEN_SIGNALS_FILE = os.path.join(SIGNALS_DIR, "_seen_signal_keys.json")
_SEEN_SIGNALS_MAX_KEEP = 5000
_LIVE_OB_MASTER_CSV = os.path.join(EXPORTS_DIR, "live_ob_signals.csv")
_LIVE_OB_MASTER_JSON = os.path.join(EXPORTS_DIR, "live_ob_signals.json")
LIVE_OB_RESOLUTION = "15"
LIVE_OB_RESOLUTION_MINUTES = 15
LIVE_OB_LOOKBACK_DAYS = 5
LIVE_OB_AUTO_REFRESH_SECONDS = 180


def _ensure_app_folders() -> None:
    """Create all required application folders if they do not already exist."""
    for folder in (SIGNALS_DIR, SIGNALS_BUY_DIR, SIGNALS_SELL_DIR, LOGS_DIR, CHARTS_DIR, EXPORTS_DIR):
        os.makedirs(folder, exist_ok=True)


_ensure_app_folders()

logger = logging.getLogger("nse_ai_pro_scanner")
logger.setLevel(logging.INFO)
if not logger.handlers:
    try:
        _file_handler = logging.FileHandler(os.path.join(LOGS_DIR, "scanner.log"), encoding="utf-8")
        _file_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))
        logger.addHandler(_file_handler)
    except OSError:
        logger.addHandler(logging.StreamHandler())

from datetime import time as _dtime
_NSE_MARKET_CLOSE_IST = _dtime(15, 30, 0)


def _now_ist() -> datetime:
    """Return the current time localized to Asia/Kolkata (IST)."""
    return datetime.now(IST)


def _format_signal_timestamp(ts, is_daily: bool = False) -> Tuple[str, str]:
    """Format a pandas/py Timestamp into (date_str, time_str) in IST."""
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    ts_ist = ts.tz_convert(IST)
    if is_daily:
        ts_ist = ts_ist.replace(hour=_NSE_MARKET_CLOSE_IST.hour, minute=_NSE_MARKET_CLOSE_IST.minute, second=_NSE_MARKET_CLOSE_IST.second, microsecond=0)
    return ts_ist.strftime("%d-%b-%Y"), ts_ist.strftime("%H:%M:%S") + " IST"


def _candle_signal_timestamp(df, is_daily: bool = False) -> Tuple[str, str]:
    """Convenience wrapper: format the timestamp of the last candle in df."""
    return _format_signal_timestamp(df["Time"].iloc[-1], is_daily=is_daily)


_HISTORY_MAX_RETRIES = 3
_HISTORY_BASE_DELAY_SECONDS = 1.0


def _safe_history(fyers, params: dict, max_retries: int = _HISTORY_MAX_RETRIES, base_delay: float = _HISTORY_BASE_DELAY_SECONDS):
    """
    Resilient wrapper around fyers.history() with retry/backoff logic.
    Unchanged core retry logic — only type hints/comments added.
    """
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


_VALID_EQ_SYMBOL_RE = re.compile(r"^NSE:[A-Z0-9&\-]+-EQ$")


def _validate_symbols(symbols) -> List[str]:
    """De-duplicate and validate a list of NSE equity symbols."""
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


class ScanStats:
    """Tracks progress/summary counters for a single scan run."""

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
    """Render the standard 6-column scan summary metrics bar."""
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Total Stocks", stats.total)
    c2.metric("Scanned", stats.scanned)
    c3.metric("Successful", stats.successful)
    c4.metric("Skipped", stats.skipped)
    c5.metric("Failed", stats.failed)
    c6.metric("Scan Time", f"{stats.elapsed_seconds:.1f}s")


@st.cache_data(ttl=60 * 60 * 12)
def load_nse_equity_symbols() -> List[str]:
    """Download and parse the Fyers NSE cash-market symbol master (cached 12h)."""
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
        st.error("Could not locate the trading-symbol column in the Fyers symbol master.")
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


FYERS_NSE_FO_SYMBOL_MASTER = "https://public.fyers.in/sym_details/NSE_FO.csv"
_FO_INDEX_UNDERLYINGS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50", "NIFTYIT", "NIFTYPSE", "NIFTYINFRA", "SENSEX", "BANKEX", "NIFTY50"}


@st.cache_data(ttl=60 * 60 * 12)
def load_nse_fo_stock_symbols() -> List[str]:
    """Download and parse the Fyers NSE F&O symbol master, mapped to equity symbols (cached 12h)."""
    try:
        resp = requests.get(FYERS_NSE_FO_SYMBOL_MASTER, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        st.error(f"Could not download Fyers F&O symbol master: {e}")
        return []
    lines = [ln for ln in resp.text.strip().split("\n") if ln.strip()]
    if not lines:
        return []
    sample = lines[:min(500, len(lines))]
    split_sample = [ln.split(",") for ln in sample]
    max_cols = max((len(p) for p in split_sample), default=0)
    best_col, best_hits = None, 0
    for col_idx in range(max_cols):
        hits = sum(1 for parts in split_sample if len(parts) > col_idx and parts[col_idx].strip().startswith("NSE:"))
        if hits > best_hits:
            best_col, best_hits = col_idx, hits
    if best_col is None or best_hits == 0:
        st.error("Could not locate the trading-symbol column in the Fyers F&O symbol master.")
        return []
    underlyings = set()
    for line in lines:
        parts = line.split(",")
        if len(parts) <= best_col:
            continue
        sym = parts[best_col].strip()
        if not sym.startswith("NSE:"):
            continue
        body = sym[len("NSE:"):]
        m = re.match(r"^([A-Z&\-]+)", body)
        if not m:
            continue
        underlying = m.group(1).strip("-")
        if underlying and underlying not in _FO_INDEX_UNDERLYINGS:
            underlyings.add(underlying)
    equity_symbols = load_nse_equity_symbols()
    equity_lookup = {s.replace("NSE:", "").replace("-EQ", ""): s for s in equity_symbols}
    fo_stock_symbols = sorted({equity_lookup[u] for u in underlyings if u in equity_lookup})
    return sorted(set(_validate_symbols(fo_stock_symbols)))


@st.cache_data(ttl=60 * 30)
def fetch_nifty_benchmark(_fyers):
    """Fetch daily NIFTY50 close series for relative-strength calculations (cached 30 min)."""
    try:
        resp, err = _safe_history(_fyers, {"symbol": NIFTY_BENCHMARK_SYMBOL, "resolution": "D", "date_format": "1", "range_from": DATE_FROM, "range_to": DATE_TO, "cont_flag": "1"})
        if err or not resp:
            return None
        candles = resp.get("candles")
        if not candles:
            return None
        ndf = pd.DataFrame(candles, columns=["Time", "Open", "High", "Low", "Close", "Volume"])
        ndf["Time"] = pd.to_datetime(ndf["Time"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
        return ndf["Close"]
    except Exception:
        return None


def calculate_rsi(close, period: int = 14):
    """Wilder's RSI via exponential moving average of gains/losses."""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return (100 - (100 / (1 + rs))).fillna(50)


def calculate_macd(close):
    """Standard 12/26/9 MACD line, signal line, and histogram."""
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    return macd_line, signal_line, macd_line - signal_line


def calculate_atr(df, period: int = 14):
    """Average True Range via Wilder smoothing."""
    h, l, c = df["High"], df["Low"], df["Close"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def calculate_supertrend(df, period: int = 10, multiplier: float = 3.0):
    """Supertrend indicator. Returns (label, is_bullish, last_value)."""
    d = df.reset_index(drop=True)
    atr_series = calculate_atr(d, period)
    valid_start = atr_series.first_valid_index()
    if valid_start is None or len(d) - valid_start < 2:
        return "N/A", None, None
    d = d.iloc[valid_start:].reset_index(drop=True)
    atr = atr_series.iloc[valid_start:].reset_index(drop=True).values
    close = d["Close"].values
    high = d["High"].values
    low = d["Low"].values
    hl2 = (high + low) / 2.0
    upperband = hl2 + multiplier * atr
    lowerband = hl2 - multiplier * atr
    n = len(d)
    final_upper = np.zeros(n)
    final_lower = np.zeros(n)
    supertrend = np.zeros(n)
    direction = np.ones(n, dtype=int)
    final_upper[0] = upperband[0]
    final_lower[0] = lowerband[0]
    supertrend[0] = final_upper[0]
    for i in range(1, n):
        final_upper[i] = upperband[i] if (upperband[i] < final_upper[i - 1] or close[i - 1] > final_upper[i - 1]) else final_upper[i - 1]
        final_lower[i] = lowerband[i] if (lowerband[i] > final_lower[i - 1] or close[i - 1] < final_lower[i - 1]) else final_lower[i - 1]
        if supertrend[i - 1] == final_upper[i - 1]:
            if close[i] <= final_upper[i]:
                supertrend[i] = final_upper[i]; direction[i] = -1
            else:
                supertrend[i] = final_lower[i]; direction[i] = 1
        else:
            if close[i] >= final_lower[i]:
                supertrend[i] = final_lower[i]; direction[i] = 1
            else:
                supertrend[i] = final_upper[i]; direction[i] = -1
    is_bullish = bool(direction[-1] == 1)
    return ("🟢 Buy" if is_bullish else "🔴 Sell"), is_bullish, round(float(supertrend[-1]), 2)


def calculate_vwap_approx(df, window: int = 20):
    """Approximate VWAP over the trailing `window` candles."""
    d = df.tail(window)
    typical = (d["High"] + d["Low"] + d["Close"]) / 3
    vol_sum = d["Volume"].sum()
    if vol_sum <= 0:
        return round(float(d["Close"].iloc[-1]), 2)
    return round(float((typical * d["Volume"]).sum() / vol_sum), 2)


def _safe_atr_pa(df, period: int = 14):
    """Internal ATR helper used by the institutional Price Action engine.
    Identical math to calculate_atr() but kept private/NaN-safe so every
    Price Action function below can call it without depending on call
    order elsewhere in the file."""
    h, l, c = df["High"], df["Low"], df["Close"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def _last_valid_atr_pa(df, period: int = 14) -> float:
    """Last ATR value, guaranteed positive/non-NaN (falls back to 0.5% of
    last close) so every downstream division is always safe."""
    atr_series = _safe_atr_pa(df, period)
    val = atr_series.iloc[-1] if len(atr_series) else np.nan
    if pd.isna(val) or val <= 0:
        last_close = float(df["Close"].iloc[-1]) if len(df) else 0.0
        val = max(last_close * 0.005, 0.01)
    return float(val)


def detect_chart_pattern(df) -> str:
    """
    INSTITUTIONAL PRICE ACTION ENGINE — candle pattern detector (FIXED).

    Bug fixed vs the original: only Doji/Hammer/Shooting-Star/Engulfing/
    HH-HL-LH-LL were covered, and there was no minimum size/ATR filter, so
    patterns fired on statistically meaningless micro-candles. This adds
    Pin Bar, Morning Star, Evening Star, and Marubozu, and rejects any
    candle whose total range is too small relative to ATR to mean anything
    (removes "weak candle" noise per the fix spec).
    """
    if len(df) < 5:
        return "N/A"

    atr = _last_valid_atr_pa(df)
    last = df.iloc[-1]; prev = df.iloc[-2]
    body = abs(last["Close"] - last["Open"])
    rng = last["High"] - last["Low"]
    upper_wick = last["High"] - max(last["Close"], last["Open"])
    lower_wick = min(last["Close"], last["Open"]) - last["Low"]

    # FIX: ignore statistically meaningless micro-candles entirely
    if rng < 0.15 * atr:
        return "No Clear Pattern (Low Volatility)"

    if rng > 0 and body / rng < 0.1:
        return "Doji ⚪"

    # Marubozu: body dominates the whole range, negligible wicks either side
    if rng > 0 and body / rng > 0.9 and body > 0.8 * atr:
        return "Bullish Marubozu 🟩" if last["Close"] > last["Open"] else "Bearish Marubozu 🟥"

    # Pin Bar (tighter than Hammer/Shooting Star: dominant wick, tiny opposite wick)
    if lower_wick > body * 2.5 and upper_wick < body * 0.6 and rng > 0.4 * atr:
        return "Bullish Pin Bar 📌"
    if upper_wick > body * 2.5 and lower_wick < body * 0.6 and rng > 0.4 * atr:
        return "Bearish Pin Bar 📌"

    if lower_wick > body * 2 and last["Close"] > last["Open"]:
        return "Hammer 🔨"
    if upper_wick > body * 2 and last["Close"] < last["Open"]:
        return "Shooting Star 🌠"

    prev_lo, prev_hi = min(prev["Open"], prev["Close"]), max(prev["Open"], prev["Close"])
    last_lo, last_hi = min(last["Open"], last["Close"]), max(last["Open"], last["Close"])

    if (last["Close"] > last["Open"] and prev["Close"] < prev["Open"]
            and last_hi >= prev_hi and last_lo <= prev_lo and body > 0.3 * atr):
        return "Bullish Engulfing 🟢"
    if (last["Close"] < last["Open"] and prev["Close"] > prev["Open"]
            and last_hi >= prev_hi and last_lo <= prev_lo and body > 0.3 * atr):
        return "Bearish Engulfing 🔴"

    # Morning Star / Evening Star (3-candle reversal pattern)
    if len(df) >= 3:
        c1, c2, c3 = df.iloc[-3], df.iloc[-2], df.iloc[-1]
        c1_body = abs(c1["Close"] - c1["Open"])
        c2_body = abs(c2["Close"] - c2["Open"])
        c3_body = abs(c3["Close"] - c3["Open"])
        if (c1["Close"] < c1["Open"] and c1_body > 0.4 * atr
                and c2_body < 0.4 * c1_body
                and c3["Close"] > c3["Open"] and c3_body > 0.4 * atr
                and c3["Close"] > (c1["Open"] + c1["Close"]) / 2):
            return "Morning Star ⭐"
        if (c1["Close"] > c1["Open"] and c1_body > 0.4 * atr
                and c2_body < 0.4 * c1_body
                and c3["Close"] < c3["Open"] and c3_body > 0.4 * atr
                and c3["Close"] < (c1["Open"] + c1["Close"]) / 2):
            return "Evening Star 🌆"

    recent = df.tail(5)
    if recent["High"].is_monotonic_increasing and recent["Low"].is_monotonic_increasing:
        return "Higher Highs/Lows 📈"
    if recent["High"].is_monotonic_decreasing and recent["Low"].is_monotonic_decreasing:
        return "Lower Highs/Lows 📉"
    return "No Clear Pattern"


# ══════════════════════════════════════════════════════════════════════════
# ── INSTITUTIONAL PRICE ACTION ENGINE — Swing / Structure primitives ──────
# ══════════════════════════════════════════════════════════════════════════

def _detect_swing_points(df, left: int = 2, right: int = 2,
                          atr_period: int = 14, atr_mult: float = 0.5):
    """
    FIX: the original code had no real swing-point concept at all — market
    structure was inferred from a raw rolling(10).max()/.min(), which
    reacts to every wick and produces false structure breaks.

    Returns a copy of df with 'is_swing_high' / 'is_swing_low' boolean
    columns. A bar is only ever marked True if it is the single strict
    extreme of a `left`+`right` bar fractal window AND that window is
    fully inside already-closed history — i.e. a swing near the live edge
    of the data is simply not confirmed yet (this is what prevents
    repainting / look-ahead bias). Swings closer than `atr_mult`*ATR to
    the previously kept swing of the same type are treated as the same
    swing (noise filtering) rather than counted twice.
    """
    d = df.reset_index(drop=True).copy()
    n = len(d)
    d["is_swing_high"] = False
    d["is_swing_low"] = False
    if n < left + right + 1:
        return d

    atr = _safe_atr_pa(d, atr_period).values
    high = d["High"].values
    low = d["Low"].values

    raw_highs = []
    raw_lows = []
    for i in range(left, n - right):
        window_hi = high[i - left:i + right + 1]
        window_lo = low[i - left:i + right + 1]
        if high[i] == window_hi.max() and np.sum(window_hi == window_hi.max()) == 1:
            raw_highs.append(i)
        if low[i] == window_lo.min() and np.sum(window_lo == window_lo.min()) == 1:
            raw_lows.append(i)

    def _atr_filter(idxs, values):
        kept = []
        for idx in idxs:
            a = atr[idx] if not np.isnan(atr[idx]) and atr[idx] > 0 else (d["Close"].iloc[idx] * 0.005)
            if kept and abs(values[idx] - values[kept[-1]]) < atr_mult * a:
                continue
            kept.append(idx)
        return kept

    kept_highs = _atr_filter(raw_highs, high)
    kept_lows = _atr_filter(raw_lows, low)

    d.loc[kept_highs, "is_swing_high"] = True
    d.loc[kept_lows, "is_swing_low"] = True
    return d


def _classify_swing_structure(df, left: int = 2, right: int = 2):
    """
    FIX: adds the HH/HL/LH/LL classification that did not previously exist
    anywhere in the file. Returns (label, last_swing_high_value,
    last_swing_low_value) describing the most recently CONFIRMED swing
    relative to the swing of the same type immediately before it.
    """
    d = _detect_swing_points(df, left=left, right=right)
    highs = d.loc[d["is_swing_high"], "High"].tolist()
    lows = d.loc[d["is_swing_low"], "Low"].tolist()

    last_high = highs[-1] if highs else None
    last_low = lows[-1] if lows else None

    label = "N/A"
    if len(highs) >= 2 and len(lows) >= 2:
        d_highs = d.loc[d["is_swing_high"]].index.tolist()
        d_lows = d.loc[d["is_swing_low"]].index.tolist()
        if d_highs and (not d_lows or d_highs[-1] > d_lows[-1]):
            label = "HH" if highs[-1] > highs[-2] else "LH"
        elif d_lows:
            label = "HL" if lows[-1] > lows[-2] else "LL"
    return label, last_high, last_low


def calculate_mtf_trend(df) -> str:
    """Multi-timeframe (weekly vs daily) trend alignment check."""
    d = df.set_index("Time")
    weekly = d["Close"].resample("W").last().dropna()
    if len(weekly) < 6:
        return "N/A"
    w_span = min(20, max(len(weekly) - 1, 2))
    weekly_bullish = bool(weekly.iloc[-1] > weekly.ewm(span=w_span, adjust=False).mean().iloc[-1])
    daily_ema20 = df["Close"].ewm(span=20, adjust=False).mean().iloc[-1]
    daily_bullish = bool(df["Close"].iloc[-1] > daily_ema20)
    if weekly_bullish and daily_bullish:
        return "🟢 Aligned Bullish"
    if not weekly_bullish and not daily_bullish:
        return "🔴 Aligned Bearish"
    return "🟡 Mixed"


def calculate_relative_strength(close, nifty_close, period: int = 10) -> str:
    """Stock return vs NIFTY return over `period` candles."""
    if nifty_close is None or len(nifty_close) < period + 1 or len(close) < period + 1:
        return "N/A"
    stock_ret = (close.iloc[-1] / close.iloc[-period - 1] - 1) * 100
    nifty_ret = (nifty_close.iloc[-1] / nifty_close.iloc[-period - 1] - 1) * 100
    rs = stock_ret - nifty_ret
    if rs > 2:
        return f"🟢 Outperform ({rs:+.1f}%)"
    if rs < -2:
        return f"🔴 Underperform ({rs:+.1f}%)"
    return f"🟡 Inline ({rs:+.1f}%)"


def calculate_target_stoploss(last_close: float, atr: float, direction: str) -> Tuple[float, float]:
    """ATR-based target/stoploss pair for the daily 'Full Scanner' signal."""
    if pd.isna(atr) or atr <= 0:
        atr = last_close * 0.01
    if direction == "Bullish":
        return round(last_close + 2 * atr, 2), round(last_close - 1 * atr, 2)
    elif direction == "Bearish":
        return round(last_close - 2 * atr, 2), round(last_close + 1 * atr, 2)
    return round(last_close + 1.5 * atr, 2), round(last_close - 1.5 * atr, 2)


def _format_rvol_display(rvol_raw: float) -> str:
    """Add fire emojis to RVOL display for visually flagging high relative volume."""
    display = f"{rvol_raw:.2f}x"
    if rvol_raw >= 3.0:
        display += " 🔥🔥"
    elif rvol_raw >= 2.0:
        display += " ❤️‍🔥"
    return display


def calculate_ai_trend(ai_score: float) -> Tuple[str, float]:
    """Map raw AI score (0-100) into a labeled trend + confidence pair."""
    if ai_score >= 65:
        return "📈 Bullish", round(ai_score, 1)
    if ai_score <= 40:
        return "📉 Bearish", round(100 - ai_score, 1)
    return "➖ Neutral", round(50 + abs(ai_score - 50), 1)


NEWS_API_ENABLED = bool(os.environ.get("NEWS_API_KEY"))


def fetch_news_sentiment_live(stock_ticker: str):
    """Placeholder for a live news-sentiment integration (not wired up)."""
    return None


def calculate_news(stock_ticker: str, gap_pct: float, rvol: float, breakout: str) -> str:
    """Proxy 'news sentiment' label derived from gap/volume/breakout heuristics."""
    live = fetch_news_sentiment_live(stock_ticker)
    if live is not None:
        return live
    big_move = abs(gap_pct) >= 2 and rvol >= 2 and breakout != "NO"
    mild_move = abs(gap_pct) >= 1 or rvol >= 1.8
    if big_move:
        return "🟢 Positive News" if gap_pct > 0 else "🔴 Negative News"
    if mild_move:
        return "🟡 Neutral News"
    return "⚪ No Recent News"


def _rule_based_xgb_score(df, rsi_val, macd_bullish, supertrend_bullish, vwap_val, rvol, support, resistance) -> float:
    """Deterministic rule-based fallback score used when XGBoost isn't available/trained."""
    last_close = float(df["Close"].iloc[-1])
    score = 50.0
    if len(df) >= 10:
        roc = (last_close / float(df["Close"].iloc[-10]) - 1) * 100
        score += max(min(roc * 2, 15), -15)
    score += (rsi_val - 50) * 0.3
    score += 8 if macd_bullish else -8
    if supertrend_bullish is True:
        score += 8
    elif supertrend_bullish is False:
        score -= 8
    if vwap_val:
        score += 5 if last_close > vwap_val else -5
    if rvol and rvol >= 2:
        score += 5 if score >= 50 else -5
    if pd.notna(resistance) and resistance > 0 and (resistance - last_close) / last_close < 0.02:
        score -= 4
    if pd.notna(support) and support > 0 and (last_close - support) / last_close < 0.02:
        score += 4
    return max(0.0, min(100.0, score))


def _score_to_trend_label(score: float) -> str:
    if score >= 75:
        return "🟢 Strong Bullish"
    if score >= 58:
        return "🟢 Bullish"
    if score >= 42:
        return "🟡 Neutral"
    if score >= 25:
        return "🔴 Bearish"
    return "🔴 Strong Bearish"


def calculate_xgboost_prediction(df, rsi_val=None, macd_bullish=None, supertrend_bullish=None, vwap_val=None, rvol=None, support=None, resistance=None, use_ml=True) -> Tuple[str, float]:
    """XGBoost-backed (or rule-based fallback) trend prediction. Logic unchanged."""
    try:
        close = df["Close"]
        if rsi_val is None:
            rsi_val = float(calculate_rsi(close).iloc[-1])
        if macd_bullish is None:
            ml, sl, _ = calculate_macd(close)
            macd_bullish = bool(ml.iloc[-1] > sl.iloc[-1])
        if supertrend_bullish is None:
            _, supertrend_bullish, _ = calculate_supertrend(df)
        if vwap_val is None:
            vwap_val = calculate_vwap_approx(df)
        if rvol is None:
            vol_avg20 = df["Volume"].tail(20).mean()
            rvol = (df["Volume"].iloc[-1] / vol_avg20) if vol_avg20 > 0 else 0
        if support is None or resistance is None:
            resistance = df["High"].rolling(20).max().shift(1).iloc[-1]
            support = df["Low"].rolling(20).min().shift(1).iloc[-1]
        rule_score = _rule_based_xgb_score(df, rsi_val, macd_bullish, supertrend_bullish, vwap_val, rvol, support, resistance)
        if XGBOOST_AVAILABLE and os.path.exists(XGB_MODEL_PATH):
            try:
                model = xgb.XGBClassifier(); model.load_model(XGB_MODEL_PATH)
                d = df.copy().reset_index(drop=True)
                d["Return"] = d["Close"].pct_change(); d["RSI"] = calculate_rsi(d["Close"])
                _, _, hist = calculate_macd(d["Close"]); d["MACD_Hist"] = hist
                d["Vol_Ratio"] = d["Volume"] / d["Volume"].rolling(20).mean()
                d["EMA_Dist"] = d["Close"] / d["Close"].ewm(span=20, adjust=False).mean() - 1
                fc = ["Return", "RSI", "MACD_Hist", "Vol_Ratio", "EMA_Dist"]
                latest = d.dropna(subset=fc).iloc[[-1]]
                if not latest.empty:
                    proba = model.predict_proba(latest[fc])[0]
                    blended = 0.7 * float(proba[1]) * 100 + 0.3 * rule_score
                    return _score_to_trend_label(blended), round(float(max(proba)) * 100, 1)
            except Exception:
                pass
        if use_ml and XGBOOST_AVAILABLE and len(df) >= 100:
            try:
                d = df.copy().reset_index(drop=True)
                d["Return"] = d["Close"].pct_change(); d["RSI"] = calculate_rsi(d["Close"])
                _, _, hist = calculate_macd(d["Close"]); d["MACD_Hist"] = hist
                d["Vol_Ratio"] = d["Volume"] / d["Volume"].rolling(20).mean()
                d["EMA_Dist"] = d["Close"] / d["Close"].ewm(span=20, adjust=False).mean() - 1
                d["Target"] = (d["Close"].shift(-1) > d["Close"]).astype(int)
                fc = ["Return", "RSI", "MACD_Hist", "Vol_Ratio", "EMA_Dist"]
                d = d.dropna(subset=fc)
                if len(d) >= 60:
                    train = d.iloc[:-1]; latest = d.iloc[[-1]]
                    X_train, y_train = train[fc], train["Target"]
                    if y_train.nunique() >= 2:
                        model = xgb.XGBClassifier(n_estimators=50, max_depth=3, learning_rate=0.1, eval_metric="logloss", verbosity=0)
                        model.fit(X_train, y_train)
                        proba = model.predict_proba(latest[fc])[0]
                        blended = 0.6 * float(proba[1]) * 100 + 0.4 * rule_score
                        return _score_to_trend_label(blended), round(float(max(proba)) * 100, 1)
            except Exception:
                pass
        confidence = max(35.0, min(97.0, round(45 + abs(rule_score - 50) * 1.1, 1)))
        return _score_to_trend_label(rule_score), confidence
    except Exception:
        return "🟡 Neutral", 50.0


def generate_alerts(rvol, breakout, cisd_signal, mtf_trend, gap_pct) -> str:
    """Comma-joined list of quick alert badges for a scan row."""
    alerts = []
    if rvol >= 2:
        alerts.append("🔥 Volume Spike")
    if breakout != "NO":
        alerts.append("🚀 Breakout")
    if cisd_signal != "None":
        alerts.append("⚡ CISD")
    if "Aligned" in mtf_trend:
        alerts.append("📊 MTF Aligned")
    if abs(gap_pct) >= 2:
        alerts.append("↕️ Big Gap")
    return ", ".join(alerts) if alerts else "—"


def calculate_final_signal(ai_score, xgb_trend, mtf_trend, rs_label, rsi, macd_bullish, supertrend_bullish, breakout, cisd_signal, smc_structure) -> str:
    """Weighted-vote composite signal used by the 'Full Scanner' tab. Logic unchanged."""
    score = 0
    if ai_score > 70:
        score += 2
    elif ai_score > 55:
        score += 1
    elif ai_score < 30:
        score -= 2
    elif ai_score < 45:
        score -= 1
    if "Strong Bullish" in xgb_trend:
        score += 2
    elif "Bullish" in xgb_trend:
        score += 1
    elif "Strong Bearish" in xgb_trend:
        score -= 2
    elif "Bearish" in xgb_trend:
        score -= 1
    if "Aligned Bullish" in mtf_trend:
        score += 1
    elif "Aligned Bearish" in mtf_trend:
        score -= 1
    if "Outperform" in rs_label:
        score += 1
    elif "Underperform" in rs_label:
        score -= 1
    if rsi > 70:
        score -= 1
    elif rsi < 30:
        score += 1
    score += 1 if macd_bullish else -1
    if supertrend_bullish is True:
        score += 1
    elif supertrend_bullish is False:
        score -= 1
    if "Bullish" in breakout:
        score += 1
    elif "Bearish" in breakout:
        score -= 1
    if "Bullish" in cisd_signal:
        score += 1
    elif "Bearish" in cisd_signal:
        score -= 1
    if "📈" in smc_structure or "🐂" in smc_structure:
        score += 1
    elif "📉" in smc_structure or "🐻" in smc_structure:
        score -= 1
    if score >= 5:
        return "🟢 Strong Buy"
    if score >= 2:
        return "🔵 Buy"
    if score > -2:
        return "🟡 Wait"
    if score > -5:
        return "🟠 Sell"
    return "🔴 Strong Sell"


SIGNAL_QUALITY_MIN_CONFIRMATIONS = 6


def _calculate_signal_quality(ema20, ema50, rsi_val, macd_bullish, supertrend_bullish, vwap_val, last_close, rvol_raw, breakout, cisd_signal, smc_structure, last_volume, vol_avg20):
    """10-point BUY/SELL confirmation confluence engine. Logic unchanged."""
    rvol_ok = bool(rvol_raw and rvol_raw >= 1.5)
    volume_ok = bool(vol_avg20 and vol_avg20 > 0 and last_volume > vol_avg20)
    bull_checks = {
        "Bullish CISD": "Bullish" in cisd_signal,
        "BOS Confirmed": smc_structure in ("BOS 📈", "CHOCH 🐂"),
        "EMA20 > EMA50": ema20 > ema50,
        "MACD Bullish": macd_bullish is True,
        "Supertrend Buy": supertrend_bullish is True,
        "VWAP Support": vwap_val is not None and last_close > vwap_val,
        "RSI Bullish (50-80)": 50 < rsi_val < 80,
        "High RVOL": rvol_ok,
        "Breakout": breakout == "📈 Bullish",
        "Strong Volume": volume_ok,
    }
    bear_checks = {
        "Bearish CISD": "Bearish" in cisd_signal,
        "CHOCH/BOS Down": smc_structure in ("BOS 📉", "CHOCH 🐻"),
        "EMA20 < EMA50": ema20 < ema50,
        "MACD Bearish": macd_bullish is False,
        "Supertrend Sell": supertrend_bullish is False,
        "VWAP Resistance": vwap_val is not None and last_close < vwap_val,
        "RSI Bearish (20-50)": 20 < rsi_val < 50,
        "High RVOL": rvol_ok,
        "Breakdown": breakout == "📉 Bearish",
        "Strong Volume": volume_ok,
    }
    bull_count = sum(bull_checks.values())
    bear_count = sum(bear_checks.values())
    if bull_count >= bear_count:
        direction = "BUY"; confirmed_count = bull_count
        reasons = [label for label, ok in bull_checks.items() if ok]
    else:
        direction = "SELL"; confirmed_count = bear_count
        reasons = [label for label, ok in bear_checks.items() if ok]
    is_high_quality = confirmed_count >= SIGNAL_QUALITY_MIN_CONFIRMATIONS
    if confirmed_count >= 10:
        star_rating = "★★★★★ Very Strong"
    elif confirmed_count >= 8:
        star_rating = "★★★★ Strong"
    elif confirmed_count >= 6:
        star_rating = "★★★ Medium"
    elif confirmed_count >= 4:
        star_rating = "★★ Weak"
    else:
        star_rating = "★ Very Weak"
    reason_str = ", ".join(reasons) if reasons else "No strong confluence"
    return direction, confirmed_count, is_high_quality, star_rating, reason_str


def _determine_entry_and_decision(direction, confirmed_count, ai_score, confidence, rvol_raw, volume_ok):
    """Translate quality-vote counts into an entry-confirmation decision. Logic unchanged."""
    trend_confirmed = confirmed_count >= SIGNAL_QUALITY_MIN_CONFIRMATIONS
    strict_buy = direction == "BUY" and ai_score >= 80 and confidence >= 75 and rvol_raw >= 1.5 and volume_ok and trend_confirmed
    strict_sell = direction == "SELL" and ai_score <= 20 and confidence >= 75 and rvol_raw >= 1.5 and volume_ok and trend_confirmed
    if strict_buy:
        return "✅ Confirmed BUY", "🟢 High Probability" if confirmed_count >= 8 else "🟡 Medium Probability", "🟢 BUY"
    if strict_sell:
        return "❌ Avoid Trade", "🟢 High Probability" if confirmed_count >= 8 else "🟡 Medium Probability", "🔴 SELL"
    tq = "🟢 High Probability" if confirmed_count >= 8 else ("🟡 Medium Probability" if confirmed_count >= 6 else "🔴 Low Probability")
    return "⚠️ Wait for Confirmation", tq, "🟡 WAIT"


def _calculate_smc_and_cisd(df):
    """
    INSTITUTIONAL PRICE ACTION ENGINE — Smart Money Concepts / CISD (FIXED).

    Bugs fixed vs the original:
      1. "BOS" was `Close > rolling(10).max().shift(1)` — a single abnormal
         wick-driven bar could trip it, no volume confirmation, and it used
         a fixed rolling window instead of real confirmed swing points.
      2. "CISD" compared only the immediately preceding bar's High/Low —
         pure noise that re-triggered on almost every bar in a chop market,
         with no requirement that price actually CLOSE beyond the level.
      3. No CHOCH concept existed. A CHOCH is only real when a BOS occurs
         OPPOSITE to the currently established trend — this now tracks
         trend state explicitly, and suppresses CHOCH labels generated
         inside a genuinely ranging market (range < 2.5x ATR).

    BOS requires: (a) Close beyond the last CONFIRMED swing high/low, not
    merely touched by a wick, (b) volume on the breaking candle above its
    20-period average. Same return shape as before:
    (smc_structure: str, cisd_signal: str, event_ts: Optional[Timestamp])
    """
    if len(df) < 30:
        return "Range ➖", "None", None

    d = df.reset_index(drop=True).copy()
    swings = _detect_swing_points(d, left=2, right=2)
    atr = _safe_atr_pa(d, 14)
    vol_avg20 = d["Volume"].rolling(20, min_periods=5).mean()

    swing_highs = swings.index[swings["is_swing_high"]].tolist()
    swing_lows = swings.index[swings["is_swing_low"]].tolist()

    smc_structure = "Range ➖"
    cisd_signal = "None"
    event_idx = None
    event_ts = None

    lookback_start = max(30, len(d) - 60)
    last_trend = None

    for i in range(lookback_start, len(d)):
        close_i = d["Close"].iloc[i]
        vol_i = d["Volume"].iloc[i]
        vavg = vol_avg20.iloc[i]
        volume_confirmed = bool(pd.notna(vavg) and vavg > 0 and vol_i > vavg)

        prior_highs = [h for h in swing_highs if h < i]
        prior_lows = [l for l in swing_lows if l < i]
        if not prior_highs and not prior_lows:
            continue

        broke_up = bool(prior_highs and close_i > d["High"].iloc[prior_highs[-1]] and volume_confirmed)
        broke_down = bool(prior_lows and close_i < d["Low"].iloc[prior_lows[-1]] and volume_confirmed)

        if broke_up and not broke_down:
            is_choch = last_trend == "bearish"
            smc_structure = "CHOCH 🐂" if is_choch else "BOS 📈"
            cisd_signal = "Bullish CISD 🚀"
            last_trend = "bullish"
            event_idx = i
        elif broke_down and not broke_up:
            is_choch = last_trend == "bullish"
            smc_structure = "CHOCH 🐻" if is_choch else "BOS 📉"
            cisd_signal = "Bearish CISD 🩸"
            last_trend = "bearish"
            event_idx = i

    if "CHOCH" in smc_structure and len(d) >= 20:
        recent_range = float(d["High"].tail(20).max() - d["Low"].tail(20).min())
        recent_atr = float(atr.iloc[-1]) if pd.notna(atr.iloc[-1]) else 0.0
        if recent_atr > 0 and recent_range < 2.5 * recent_atr:
            smc_structure = smc_structure.replace("CHOCH 🐂", "BOS 📈").replace("CHOCH 🐻", "BOS 📉")

    if event_idx is not None:
        event_ts = d["Time"].iloc[event_idx]

    return smc_structure, cisd_signal, event_ts


_OB_LOOKBACK = 20
_OB_MIN_MOVE_PCT = 1.5
_OB_VOL_MULTIPLIER = 1.2


def _detect_order_blocks(df, smc_structure):
    """
    INSTITUTIONAL PRICE ACTION ENGINE — Order Block detector (FIXED).

    Bugs fixed vs the original:
      - No retest tracking at all: an OB tapped into 5 times was reported
        identically to a first-touch fresh OB.
      - "Strength" ignored how many times the zone had since been retested.
      - No minimum body-size floor, so a tiny/noise candle could seed an OB.

    Fix: after locating the candidate OB candle, walk every bar AFTER it up
    to the current bar and count re-entries into the zone. 0 = Fresh /
    Untested. 1 = Tested once (still usable, labeled). 2+ = rejected
    outright (over-tested / weak, per the "reject multiple retests" spec).
    Same return shape as before: (bullish_label, bearish_label, ob_zone, ob_strength)
    """
    if len(df) < 15:
        return "No", "No", "—", "—"

    d = df.reset_index(drop=True)
    lookback = min(_OB_LOOKBACK, len(d) - 3)
    recent = d.tail(lookback + 2).reset_index(drop=True)
    vol_avg = d["Volume"].tail(20).mean()
    last_close = float(d["Close"].iloc[-1])
    atr = _last_valid_atr_pa(d)

    bullish_label, bearish_label = "No", "No"
    ob_zone, ob_strength = "—", "—"
    is_bos_bullish = smc_structure in ("BOS 📈", "CHOCH 🐂")
    is_bos_bearish = smc_structure in ("BOS 📉", "CHOCH 🐻")

    def _strength(move_pct, candle_vol):
        if move_pct >= 4 and vol_avg > 0 and candle_vol >= vol_avg * 2:
            return "Strong"
        if move_pct >= 2.5 or (vol_avg > 0 and candle_vol >= vol_avg * 1.5):
            return "Medium"
        return "Weak"

    def _count_retests(zone_low, zone_high, after_idx) -> int:
        touches = 0
        inside_prev = False
        for j in range(after_idx + 1, len(recent)):
            lo, hi = float(recent["Low"].iloc[j]), float(recent["High"].iloc[j])
            inside = not (hi < zone_low or lo > zone_high)
            if inside and not inside_prev:
                touches += 1
            inside_prev = inside
        return touches

    try:
        if is_bos_bullish:
            for i in range(len(recent) - 2, 0, -1):
                candle = recent.iloc[i]
                body = abs(candle["Close"] - candle["Open"])
                if not (candle["Close"] < candle["Open"]) or body < 0.15 * atr:
                    continue
                if i + 1 >= len(recent):
                    continue
                move_after = recent["Close"].iloc[i + 1:].max()
                move_pct = ((move_after - candle["Close"]) / candle["Close"] * 100) if candle["Close"] else 0
                vol_ok = vol_avg > 0 and candle["Volume"] >= vol_avg * _OB_VOL_MULTIPLIER
                if move_pct >= _OB_MIN_MOVE_PCT and vol_ok:
                    zone_low, zone_high = round(float(candle["Low"]), 2), round(float(candle["High"]), 2)
                    if zone_low <= last_close <= zone_high * 1.02:
                        retests = _count_retests(zone_low, zone_high, i)
                        if retests >= 2:
                            break
                        bullish_label = "🟢 Bullish OB" if retests == 0 else "🟢 Bullish OB (Tested x1)"
                        ob_zone = f"{zone_low}–{zone_high}"
                        ob_strength = _strength(move_pct, float(candle["Volume"]))
                    break
        if is_bos_bearish and bullish_label == "No":
            for i in range(len(recent) - 2, 0, -1):
                candle = recent.iloc[i]
                body = abs(candle["Close"] - candle["Open"])
                if not (candle["Close"] > candle["Open"]) or body < 0.15 * atr:
                    continue
                if i + 1 >= len(recent):
                    continue
                move_after = recent["Close"].iloc[i + 1:].min()
                move_pct = ((candle["Close"] - move_after) / candle["Close"] * 100) if candle["Close"] else 0
                vol_ok = vol_avg > 0 and candle["Volume"] >= vol_avg * _OB_VOL_MULTIPLIER
                if move_pct >= _OB_MIN_MOVE_PCT and vol_ok:
                    zone_low, zone_high = round(float(candle["Low"]), 2), round(float(candle["High"]), 2)
                    if zone_low * 0.98 <= last_close <= zone_high:
                        retests = _count_retests(zone_low, zone_high, i)
                        if retests >= 2:
                            break
                        bearish_label = "🔴 Bearish OB" if retests == 0 else "🔴 Bearish OB (Tested x1)"
                        ob_zone = f"{zone_low}–{zone_high}"
                        ob_strength = _strength(move_pct, float(candle["Volume"]))
                    break
    except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError, AttributeError):
        return "No", "No", "—", "—"
    return bullish_label, bearish_label, ob_zone, ob_strength


def _parse_ob_zone(ob_zone):
    """Parse a 'low–high' order-block zone string back into two floats."""
    if not ob_zone or ob_zone == "—":
        return None, None
    try:
        low_str, high_str = ob_zone.split("–")
        return float(low_str), float(high_str)
    except (ValueError, AttributeError):
        return None, None


# ══════════════════════════════════════════════════════════════════════════
# ── ENHANCED SIGNAL VALIDATION ENGINE (institutional price action) ────────
# ══════════════════════════════════════════════════════════════════════════

def calculate_adx(df, period: int = 14):
    """ADX. Returns (ADX, +DI, -DI). ADX>25=trending, <20=sideways."""
    h, l, c = df["High"], df["Low"], df["Close"]
    up_move = h.diff(); down_move = -l.diff()
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=df.index)
    atr_s = calculate_atr(df, period)
    plus_di = 100 * (plus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr_s.replace(0, np.nan)).fillna(0)
    minus_di = 100 * (minus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr_s.replace(0, np.nan)).fillna(0)
    dx = (100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)).fillna(0)
    adx = dx.ewm(alpha=1 / period, adjust=False).mean()
    return round(float(adx.iloc[-1]), 1), round(float(plus_di.iloc[-1]), 1), round(float(minus_di.iloc[-1]), 1)


def detect_fvg(df, trend_hint=None) -> dict:
    """
    INSTITUTIONAL PRICE ACTION ENGINE — Fair Value Gap detector (FIXED).

    Bugs fixed vs the original:
      - Mitigated (>=50% filled) gaps were still returned and labeled
        "Filled" as if they were a usable signal input — the fix spec
        explicitly requires rejecting filled FVGs.
      - No minimum gap-size floor, so 0.01%-of-price rounding noise was
        treated the same as a genuine imbalance.
      - Direction was never checked against the prevailing trend.

    Only ever returns an UNMITIGATED gap above a minimum ATR-relative
    size. `trend_hint` ('Bullish'|'Bearish'|None) is a new OPTIONAL kwarg
    — existing callers that don't pass it keep the old behaviour (accepts
    either direction). Same return shape as before (dict).
    """
    empty = {"label": "No FVG", "type": None, "gap_size": 0.0, "filled_pct": 0.0,
              "age_candles": None, "freshness": "—", "mitigated": False, "nearest_dist": None}
    if len(df) < 5:
        return empty

    last_close = float(df["Close"].iloc[-1])
    atr = _last_valid_atr_pa(df)
    min_gap = max(0.1 * atr, last_close * 0.0008)

    recent = df.tail(30).reset_index(drop=True)
    n = len(recent)
    best = None
    for i in range(1, n - 1):
        prev_high = float(recent["High"].iloc[i - 1]); prev_low = float(recent["Low"].iloc[i - 1])
        next_high = float(recent["High"].iloc[i + 1]); next_low = float(recent["Low"].iloc[i + 1])
        bullish = prev_high < next_low
        bearish = prev_low > next_high
        if not bullish and not bearish:
            continue
        if bullish:
            gap_low, gap_high, direction = prev_high, next_low, "Bullish"
        else:
            gap_low, gap_high, direction = next_high, prev_low, "Bearish"
        gap_size = round(gap_high - gap_low, 4)
        if gap_size < min_gap:
            continue
        if trend_hint in ("Bullish", "Bearish") and direction != trend_hint:
            continue

        age_candles = n - 1 - i
        fill_depth = max(0.0, last_close - gap_low) if bullish else max(0.0, gap_high - last_close)
        filled_pct = round(min(fill_depth / gap_size * 100, 100), 1) if gap_size > 0 else 100.0
        mitigated = filled_pct >= 50.0
        if mitigated:
            continue
        freshness = "Old" if age_candles > 10 else "Fresh"
        if best is None or age_candles < best["age_candles"]:
            best = {"type": direction, "gap_low": round(gap_low, 2), "gap_high": round(gap_high, 2),
                     "gap_size": round(gap_size, 2), "filled_pct": filled_pct, "age_candles": age_candles,
                     "freshness": freshness, "mitigated": mitigated,
                     "nearest_dist": round(abs(last_close - (gap_low if bullish else gap_high)), 2)}

    if best is None:
        return empty
    best["label"] = f"{best['type']} Fresh"
    return best


def classify_order_block(df, smc_structure, fvg) -> dict:
    """Enhanced OB: Fresh/Mitigated/Institutional/Retail classification. Logic unchanged
    (now benefits automatically from the fixed _detect_order_blocks above)."""
    bullish_ob, bearish_ob, ob_zone, ob_strength = _detect_order_blocks(df, smc_structure)
    last_close = float(df["Close"].iloc[-1])

    def _ob_meta(ob_label, side):
        if ob_label == "No":
            return {"label": f"No {side} OB", "fresh": False, "institutional": False, "ob_type": "None"}
        zone_low, zone_high = _parse_ob_zone(ob_zone)
        if zone_low is None:
            return {"label": ob_label, "fresh": False, "institutional": False, "ob_type": "Unknown"}
        fresh = not (zone_low <= last_close <= zone_high)
        institutional = ob_strength == "Strong" and fvg.get("type") == side and not fvg.get("mitigated", True)
        retail = ob_strength == "Weak" or fvg.get("type") != side
        if institutional:
            ob_type, prefix = "Institutional", "Institutional"
        elif retail:
            ob_type, prefix = "Retail", "Weak"
        else:
            ob_type, prefix = "Strong", "Strong"
        return {"label": f"{'Fresh' if fresh else 'Mitigated'} {prefix} {side} OB", "fresh": fresh, "institutional": institutional, "ob_type": ob_type}

    bull_meta = _ob_meta(bullish_ob, "Bullish")
    bear_meta = _ob_meta(bearish_ob, "Bearish")
    return {"bullish_ob_label": bull_meta["label"], "bearish_ob_label": bear_meta["label"], "ob_zone": ob_zone, "ob_strength": ob_strength, "bull_ob_fresh": bull_meta["fresh"], "bear_ob_fresh": bear_meta["fresh"], "bull_ob_institutional": bull_meta["institutional"], "bear_ob_institutional": bear_meta["institutional"], "bear_ob_type": bear_meta["ob_type"], "bull_ob_type": bull_meta["ob_type"]}


def detect_liquidity_sweep(df) -> Tuple[str, str]:
    """
    INSTITUTIONAL PRICE ACTION ENGINE — Liquidity Sweep detector (FIXED).

    Bugs fixed vs the original:
      - Only checked the single most recent bar's high/low against a swing
        range computed from the same trailing window (self-referential and
        noisy), no Equal-High/Equal-Low pool detection, and no requirement
        that the reversal actually continue on the NEXT bar (so a sweep
        that immediately failed still counted as a valid signal).

    Adds Equal-High/Equal-Low liquidity-pool detection (2+ swing highs/lows
    within a tight ATR-relative tolerance) and requires the bar AFTER the
    sweep bar to confirm continuation before it is labeled "confirmed".
    Same return shape as before: (label, side)
    """
    if len(df) < 20:
        return "No Sweep", "None"

    d = df.reset_index(drop=True)
    atr = _last_valid_atr_pa(d)
    swings = _detect_swing_points(d.tail(40).reset_index(drop=True), left=2, right=2)
    swing_highs = swings.loc[swings["is_swing_high"], "High"].tolist()
    swing_lows = swings.loc[swings["is_swing_low"], "Low"].tolist()

    def _has_equal_pool(levels, tol):
        levels = sorted(levels)
        for a, b in zip(levels, levels[1:]):
            if abs(a - b) <= tol:
                return True
        return False

    eq_high_pool = _has_equal_pool(swing_highs[-4:], 0.25 * atr) if len(swing_highs) >= 2 else False
    eq_low_pool = _has_equal_pool(swing_lows[-4:], 0.25 * atr) if len(swing_lows) >= 2 else False

    recent = d.tail(20).reset_index(drop=True)
    if len(recent) < 4:
        return "No Sweep", "None"

    swing_low_ref = float(recent["Low"].iloc[:-3].min())
    swing_high_ref = float(recent["High"].iloc[:-3].max())

    sweep_idx = len(recent) - 2
    confirm_idx = len(recent) - 1

    sweep_low = float(recent["Low"].iloc[sweep_idx])
    sweep_high = float(recent["High"].iloc[sweep_idx])
    sweep_close = float(recent["Close"].iloc[sweep_idx])
    confirm_close = float(recent["Close"].iloc[confirm_idx])

    if sweep_low < swing_low_ref and sweep_close > swing_low_ref:
        confirmed = confirm_close >= sweep_close
        pool_tag = " (Equal Lows)" if eq_low_pool else ""
        label = f"🔽 Sell-Side Sweep (Buy Setup){pool_tag}" if confirmed else f"🔽 Sell-Side Sweep — Unconfirmed{pool_tag}"
        return label, ("Buy" if confirmed else "None")

    if sweep_high > swing_high_ref and sweep_close < swing_high_ref:
        confirmed = confirm_close <= sweep_close
        pool_tag = " (Equal Highs)" if eq_high_pool else ""
        label = f"🔼 Buy-Side Sweep (Sell Setup){pool_tag}" if confirmed else f"🔼 Buy-Side Sweep — Unconfirmed{pool_tag}"
        return label, ("Sell" if confirmed else "None")

    return "No Sweep", "None"


def detect_htf_trend(df) -> str:
    """HTF trend via monthly resampled daily candles. Logic unchanged."""
    if len(df) < 60:
        return "Insufficient Data"
    d = df.set_index("Time")
    monthly = d["Close"].resample("ME").last().dropna()
    if len(monthly) < 4:
        return "N/A"
    span = min(6, len(monthly) - 1)
    ema = monthly.ewm(span=span, adjust=False).mean()
    if monthly.iloc[-1] > ema.iloc[-1] and monthly.iloc[-1] > monthly.iloc[-2]:
        return "🟢 HTF Bullish"
    if monthly.iloc[-1] < ema.iloc[-1] and monthly.iloc[-1] < monthly.iloc[-2]:
        return "🔴 HTF Bearish"
    return "🟡 HTF Sideways"


def calculate_momentum(df, rsi_val, macd_bullish, adx_val) -> str:
    """Composite short-term momentum label. Logic unchanged."""
    if len(df) < 10:
        return "⚪ Weak"
    roc5 = (float(df["Close"].iloc[-1]) / float(df["Close"].iloc[-5]) - 1) * 100 if len(df) >= 5 else 0
    bull = sum([roc5 > 1.0, rsi_val > 55, macd_bullish, adx_val > 25])
    bear = sum([roc5 < -1.0, rsi_val < 45, not macd_bullish, adx_val > 25])
    if bull >= 3:
        return "🟢 Strong Bullish"
    if bear >= 3:
        return "🔴 Strong Bearish"
    if bull == 2:
        return "🟡 Moderate Bullish"
    if bear == 2:
        return "🟡 Moderate Bearish"
    return "⚪ Weak"


# ══════════════════════════════════════════════════════════════════════════
# ── INSTITUTIONAL PRICE ACTION ENGINE — new structural functions ──────────
# ══════════════════════════════════════════════════════════════════════════

def _calculate_support_resistance_v2(df, lookback: int = 120) -> Dict[str, object]:
    """
    FIX: the original support/resistance was a single rolling(20).max()/
    .min() number — no merging of nearby levels (impossible with only one
    candidate), and no strength ranking.

    Gathers ALL confirmed swing highs/lows over the lookback window,
    clusters ones within an ATR-relative tolerance into one level, counts
    touches (= strength), prefers multi-touch levels over single-touch
    ones, and returns the nearest qualifying level on each side of price.
    """
    d = df.tail(lookback).reset_index(drop=True) if len(df) > lookback else df.reset_index(drop=True)
    if len(d) < 15:
        return {"support": None, "resistance": None, "support_strength": 0, "resistance_strength": 0}

    atr = _last_valid_atr_pa(d)
    merge_tol = max(0.4 * atr, float(d["Close"].iloc[-1]) * 0.002)
    last_close = float(d["Close"].iloc[-1])

    swings = _detect_swing_points(d, left=2, right=2)
    highs = sorted(swings.loc[swings["is_swing_high"], "High"].tolist())
    lows = sorted(swings.loc[swings["is_swing_low"], "Low"].tolist())

    def _cluster(levels):
        clusters = []
        for lvl in levels:
            if clusters and abs(lvl - clusters[-1]["mean"]) <= merge_tol:
                c = clusters[-1]
                c["members"].append(lvl)
                c["mean"] = float(np.mean(c["members"]))
            else:
                clusters.append({"members": [lvl], "mean": lvl})
        return [(c["mean"], len(c["members"])) for c in clusters]

    res_clusters = [c for c in _cluster(highs) if c[0] > last_close]
    sup_clusters = [c for c in _cluster(lows) if c[0] < last_close]

    def _pick_best(clusters, nearest=True):
        strong = [c for c in clusters if c[1] >= 2]
        pool = strong if strong else clusters
        if not pool:
            return None, 0
        pool.sort(key=lambda c: (-c[1], abs(c[0] - last_close)) if not nearest else abs(c[0] - last_close))
        return round(float(pool[0][0]), 2), int(pool[0][1])

    resistance, res_strength = _pick_best(res_clusters)
    support, sup_strength = _pick_best(sup_clusters)

    return {"support": support, "resistance": resistance,
            "support_strength": sup_strength, "resistance_strength": res_strength}


def _classify_trend_composite(df) -> Dict[str, object]:
    """
    FIX: trend detection must never rely on EMA alone. Combines EMA20/50/200
    alignment + swing structure (HH/HL/LH/LL) + VWAP position + ADX into one
    composite trend label and a numeric strength score (0-100).
    """
    close = df["Close"]
    ema20 = close.ewm(span=20, adjust=False).mean().iloc[-1]
    ema50 = close.ewm(span=50, adjust=False).mean().iloc[-1]
    ema200 = (close.ewm(span=200, adjust=False).mean().iloc[-1]
              if len(close) >= 200 else close.ewm(span=len(close), adjust=False).mean().iloc[-1])
    last_close = float(close.iloc[-1])

    ema_bull_votes = sum([last_close > ema20, ema20 > ema50, ema50 > ema200])
    ema_bear_votes = sum([last_close < ema20, ema20 < ema50, ema50 < ema200])

    swing_label, _, _ = _classify_swing_structure(df)
    swing_bull = swing_label in ("HH", "HL")
    swing_bear = swing_label in ("LH", "LL")

    adx_val, _, _ = calculate_adx(df) if len(df) >= 15 else (0.0, 0.0, 0.0)

    d_tail = df.tail(20)
    typical = (d_tail["High"] + d_tail["Low"] + d_tail["Close"]) / 3
    vol_sum = d_tail["Volume"].sum()
    vwap_val = float((typical * d_tail["Volume"]).sum() / vol_sum) if vol_sum > 0 else last_close
    vwap_bull = last_close > vwap_val
    vwap_bear = last_close < vwap_val

    bull_score = ema_bull_votes + int(swing_bull) + int(vwap_bull)
    bear_score = ema_bear_votes + int(swing_bear) + int(vwap_bear)

    ema20_gt_ema50_gt_ema200 = bool(ema20 > ema50 > ema200)
    ema20_lt_ema50_lt_ema200 = bool(ema20 < ema50 < ema200)

    if adx_val < 18:
        label = "🟡 Sideways / Ranging"
        strength = round(min(adx_val / 18 * 40, 40), 1)
    elif bull_score >= 4 and bull_score > bear_score:
        label = "🟢🟢 Strong Uptrend" if bull_score == 5 else "🟢 Uptrend"
        strength = round(min(40 + bull_score * 10 + adx_val * 0.3, 100), 1)
    elif bear_score >= 4 and bear_score > bull_score:
        label = "🔴🔴 Strong Downtrend" if bear_score == 5 else "🔴 Downtrend"
        strength = round(min(40 + bear_score * 10 + adx_val * 0.3, 100), 1)
    else:
        label = "🟡 Mixed / No Clear Trend"
        strength = round(30 + adx_val * 0.2, 1)

    return {"label": label, "strength": strength, "adx": round(float(adx_val), 1),
            "direction": "Bullish" if bull_score > bear_score else ("Bearish" if bear_score > bull_score else "Neutral"),
            "ema20": round(float(ema20), 2), "ema50": round(float(ema50), 2), "ema200": round(float(ema200), 2),
            "ema_stack_bullish": ema20_gt_ema50_gt_ema200, "ema_stack_bearish": ema20_lt_ema50_lt_ema200,
            "vwap": round(float(vwap_val), 2), "vwap_bullish": vwap_bull}


def _calculate_pullback_quality(df, trend_direction: str) -> str:
    """
    FIX: pullback logic did not exist at all in the original file. Rejects
    deep (>61.8%) retracements, confirms the trend leg is still intact, and
    checks volume contracts during the pullback and expands on the latest
    continuation bar.
    """
    if trend_direction not in ("Bullish", "Bearish") or len(df) < 15:
        return "N/A"

    d = df.tail(15).reset_index(drop=True)
    close = d["Close"]
    swing_start = close.iloc[0]
    extreme = close.max() if trend_direction == "Bullish" else close.min()
    last = close.iloc[-1]

    leg = abs(extreme - swing_start)
    if leg <= 0:
        return "N/A"
    retrace = abs(extreme - last) / leg * 100

    vol_first_half = d["Volume"].iloc[:len(d) // 2].mean()
    vol_second_half = d["Volume"].iloc[len(d) // 2:-1].mean() if len(d) > 3 else vol_first_half
    last_vol = float(d["Volume"].iloc[-1])
    volume_contracted = bool(vol_second_half <= vol_first_half * 1.05)
    volume_expanding_now = bool(last_vol > d["Volume"].mean())

    trend_intact = (last > swing_start) if trend_direction == "Bullish" else (last < swing_start)

    if not trend_intact:
        return "🔴 Trend Broken — Not a Pullback"
    if retrace > 61.8:
        return "🔴 Deep Retracement — Reject"
    if 23.6 <= retrace <= 61.8 and volume_contracted:
        return "🟢 Healthy Pullback" + (" + Volume Confirming" if volume_expanding_now else "")
    if retrace < 23.6:
        return "🟡 Shallow Pullback"
    return "🟡 Unconfirmed Pullback"


def _market_filter_ok(df, adx_val: float, rvol_raw: float) -> Tuple[bool, str]:
    """
    FIX: no market filter existed anywhere. Blocks signals in sideways
    (ADX<15), low-volatility (ATR%<0.25), low-volume (RVOL<0.5), or
    abnormally thin (possible holiday/half-day) sessions.
    """
    if len(df) < 20:
        return False, "Insufficient history"
    last_close = float(df["Close"].iloc[-1])
    atr = _last_valid_atr_pa(df)
    atr_pct = (atr / last_close * 100) if last_close else 0.0

    if adx_val < 15:
        return False, "Sideways market (ADX < 15)"
    if atr_pct < 0.25:
        return False, "Low volatility (ATR% too small)"
    if rvol_raw < 0.5:
        return False, "Low volume (RVOL < 0.5x)"
    if len(df) >= 5:
        last5_avg = float(df["Volume"].tail(5).mean())
        prior_avg = float(df["Volume"].tail(20).mean())
        if prior_avg > 0 and last5_avg < prior_avg * 0.3:
            return False, "Abnormally thin session volume (possible holiday/half-day)"
    return True, "OK"


def _detect_valid_breakout(df, support: Optional[float], resistance: Optional[float]) -> str:
    """
    FIX: breakout detection previously accepted wick-only or low-volume
    breaks. Requires (1) a CLOSE beyond the level (not a wick), (2) volume
    above the 20-period average, (3) true range expansion vs ATR (genuine
    momentum, not a slow drift).
    """
    if len(df) < 25 or support is None or resistance is None:
        return "NO"
    last = df.iloc[-1]
    last_close = float(last["Close"])
    atr = _last_valid_atr_pa(df)
    true_range = float(last["High"] - last["Low"])
    vol_avg20 = float(df["Volume"].tail(20).mean())
    volume_ok = bool(vol_avg20 > 0 and float(last["Volume"]) > vol_avg20)
    atr_expansion_ok = bool(atr > 0 and true_range >= 1.1 * atr)

    if last_close > resistance and volume_ok and atr_expansion_ok:
        return "📈 Bullish"
    if last_close < support and volume_ok and atr_expansion_ok:
        return "📉 Bearish"
    return "NO"


def validate_price_action_signal(*, direction: str, trend_direction: str, vwap_bull: bool,
                                   ema_aligned: bool, volume_confirmed: bool, momentum_ok: bool,
                                   atr_ok: bool, rr_ratio: float, market_ok: bool,
                                   market_reason: str) -> Tuple[str, str]:
    """
    FIX: nothing in the original file could ever output "don't trade" — the
    composite scores always resolved to some flavor of BUY/SELL/HOLD. This
    is the hard institutional gate: EVERY check must agree with `direction`
    or the engine refuses to issue a trade signal.
    Returns (decision, reject_reason) where decision is one of:
      '🟢 BUY' / '🔴 SELL' / '⏸️ WAIT' / '🚫 NO TRADE'
    """
    if not market_ok:
        return "🚫 NO TRADE", market_reason

    is_buy = direction == "BUY"
    checks = {
        "Trend agrees": (trend_direction == "Bullish") if is_buy else (trend_direction == "Bearish"),
        "VWAP agrees": vwap_bull if is_buy else (not vwap_bull),
        "EMA alignment agrees": ema_aligned,
        "Volume confirmed": volume_confirmed,
        "Momentum agrees": momentum_ok,
        "ATR/volatility sufficient": atr_ok,
        "Risk:Reward >= 1.5": rr_ratio >= 1.5,
    }
    failed = [name for name, ok in checks.items() if not ok]
    if failed:
        return "⏸️ WAIT", "Failed: " + ", ".join(failed)
    return ("🟢 BUY" if is_buy else "🔴 SELL"), "All validations passed"


def _institutional_grade(*, ema_stack_ok: bool, vwap_ok: bool, adx_val: float, rsi_agrees: bool,
                          macd_agrees: bool, volume_ok: bool, atr_ok: bool, bos_confirmed: bool,
                          fresh_ob_exists: bool, valid_fvg_exists: bool, liquidity_confirmed: bool,
                          rr_ratio: float) -> Tuple[str, int, List[str]]:
    """
    Institutional-grade checklist required by the fix spec:
      EMA20>EMA50>EMA200 (or reverse for SELL), VWAP agrees, ADX>25,
      RSI agrees, MACD agrees, Volume>20-EMA-Volume, ATR sufficient,
      BOS confirmed, Fresh OB exists, Valid FVG exists, Liquidity Sweep
      confirmed, RR>=1:2. Returns (grade, passed_count, failed_checks).
    Grades: A+ (12/12) · A (>=10) · B (>=8) · C (>=6) · REJECT (<6)
    """
    checks = {
        "EMA Stack": ema_stack_ok,
        "VWAP": vwap_ok,
        "ADX > 25": adx_val > 25,
        "RSI Agrees": rsi_agrees,
        "MACD Agrees": macd_agrees,
        "Volume > 20EMA": volume_ok,
        "ATR Sufficient": atr_ok,
        "BOS Confirmed": bos_confirmed,
        "Fresh Order Block": fresh_ob_exists,
        "Valid FVG": valid_fvg_exists,
        "Liquidity Sweep Confirmed": liquidity_confirmed,
        "RR >= 1:2": rr_ratio >= 2.0,
    }
    passed = [k for k, v in checks.items() if v]
    failed = [k for k, v in checks.items() if not v]
    n = len(passed)
    if n == 12:
        grade = "A+"
    elif n >= 10:
        grade = "A"
    elif n >= 8:
        grade = "B"
    elif n >= 6:
        grade = "C"
    else:
        grade = "REJECT"
    return grade, n, failed


def build_price_action_report_row(df, symbol: str) -> Dict[str, object]:
    """
    Master aggregator for the institutional Price Action engine. Computes
    every new report column in one call:
      Swing Structure, BOS Status, CHOCH Status, Breakout Status,
      Liquidity Sweep, Order Block, FVG, Support, Resistance,
      Trend Strength, Pullback Quality, Price Action Score,
      Price Action Confidence %, Decision, Reject Reason,
      Institutional Grade.
    Merge the returned dict into the row you already build in `_analyse()`
    / `_analyse_enhanced()` — nothing existing is overwritten.
    """
    try:
        if len(df) < 30:
            return {
                "Swing Structure": "N/A", "BOS Status": "N/A", "CHOCH Status": "N/A",
                "Breakout Status": "NO", "Liquidity Sweep": "No Sweep", "Order Block": "No",
                "FVG": "No FVG", "Support": None, "Resistance": None,
                "Trend Strength": "N/A", "Pullback Quality": "N/A",
                "Price Action Score": 0, "Price Action Confidence %": 0.0,
                "Decision": "🚫 NO TRADE", "Reject Reason": "Insufficient history (<30 candles)",
                "Institutional Grade": "REJECT",
            }

        trend = _classify_trend_composite(df)
        swing_label, _, _ = _classify_swing_structure(df)
        smc_structure, cisd_signal, _ = _calculate_smc_and_cisd(df)
        sr = _calculate_support_resistance_v2(df)
        breakout = _detect_valid_breakout(df, sr["support"], sr["resistance"])
        liquidity_label, liquidity_side = detect_liquidity_sweep(df)
        bull_ob, bear_ob, ob_zone, ob_strength = _detect_order_blocks(df, smc_structure)
        fvg = detect_fvg(df, trend_hint=trend["direction"] if trend["direction"] != "Neutral" else None)
        pullback = _calculate_pullback_quality(df, trend["direction"])

        last_close = float(df["Close"].iloc[-1])
        rsi_val = float(calculate_rsi(df["Close"]).iloc[-1])
        macd_line, macd_sig, _ = calculate_macd(df["Close"])
        macd_bullish = bool(macd_line.iloc[-1] > macd_sig.iloc[-1])
        vol_avg20 = float(df["Volume"].tail(20).mean())
        rvol_raw = round(float(df["Volume"].iloc[-1] / vol_avg20), 2) if vol_avg20 > 0 else 0.0
        atr = _last_valid_atr_pa(df)
        atr_pct = (atr / last_close * 100) if last_close else 0.0

        bos_status = smc_structure if "BOS" in smc_structure else "None"
        choch_status = smc_structure if "CHOCH" in smc_structure else "None"
        bos_confirmed = bos_status != "None" or choch_status != "None"

        bull_votes = sum([
            trend["direction"] == "Bullish", swing_label in ("HH", "HL"),
            "📈" in smc_structure or "🐂" in smc_structure, breakout == "📈 Bullish",
            "Buy" in liquidity_side, bull_ob != "No", fvg.get("type") == "Bullish",
        ])
        bear_votes = sum([
            trend["direction"] == "Bearish", swing_label in ("LH", "LL"),
            "📉" in smc_structure or "🐻" in smc_structure, breakout == "📉 Bearish",
            "Sell" in liquidity_side, bear_ob != "No", fvg.get("type") == "Bearish",
        ])
        pa_score = max(bull_votes, bear_votes)
        direction = "BUY" if bull_votes > bear_votes else ("SELL" if bear_votes > bull_votes else None)

        market_ok, market_reason = _market_filter_ok(df, trend["adx"], rvol_raw)

        if direction is None:
            pa_decision, reject_reason = "⏸️ WAIT", "No clear directional confluence"
            institutional_grade = "REJECT"
        else:
            ema_aligned = (trend["ema20"] > trend["ema50"]) if direction == "BUY" else (trend["ema20"] < trend["ema50"])
            volume_confirmed = bool(vol_avg20 > 0 and float(df["Volume"].iloc[-1]) > vol_avg20)
            momentum_ok = (rvol_raw >= 1.0)
            atr_ok = bool(atr_pct >= 0.25)
            risk = atr if atr > 0 else last_close * 0.01
            rr_ratio = round((2.0 * atr) / risk, 2) if risk > 0 else 0.0
            pa_decision, reject_reason = validate_price_action_signal(
                direction=direction, trend_direction=trend["direction"], vwap_bull=trend["vwap_bullish"],
                ema_aligned=ema_aligned, volume_confirmed=volume_confirmed, momentum_ok=momentum_ok,
                atr_ok=atr_ok, rr_ratio=rr_ratio, market_ok=market_ok, market_reason=market_reason,
            )
            rsi_agrees = (rsi_val > 50) if direction == "BUY" else (rsi_val < 50)
            macd_agrees = macd_bullish if direction == "BUY" else (not macd_bullish)
            ema_stack_ok = trend["ema_stack_bullish"] if direction == "BUY" else trend["ema_stack_bearish"]
            fresh_ob_exists = (bull_ob not in ("No",)) if direction == "BUY" else (bear_ob not in ("No",))
            valid_fvg_exists = fvg.get("type") == ("Bullish" if direction == "BUY" else "Bearish")
            liquidity_confirmed = ("Buy" in liquidity_side) if direction == "BUY" else ("Sell" in liquidity_side)
            institutional_grade, _, _ = _institutional_grade(
                ema_stack_ok=ema_stack_ok, vwap_ok=(trend["vwap_bullish"] if direction == "BUY" else not trend["vwap_bullish"]),
                adx_val=trend["adx"], rsi_agrees=rsi_agrees, macd_agrees=macd_agrees, volume_ok=volume_confirmed,
                atr_ok=atr_ok, bos_confirmed=bos_confirmed, fresh_ob_exists=fresh_ob_exists,
                valid_fvg_exists=valid_fvg_exists, liquidity_confirmed=liquidity_confirmed, rr_ratio=rr_ratio,
            )
            if institutional_grade == "REJECT" and pa_decision in ("🟢 BUY", "🔴 SELL"):
                pa_decision = "⏸️ WAIT"
                reject_reason = "Institutional checklist below minimum grade"

        confidence = round(min(100.0, (pa_score / 7) * 60 + trend["strength"] * 0.3 + (10 if market_ok else 0)), 1)

        return {
            "Swing Structure": swing_label,
            "BOS Status": bos_status,
            "CHOCH Status": choch_status,
            "Breakout Status": breakout,
            "Liquidity Sweep": liquidity_label,
            "Order Block": bull_ob if bull_ob != "No" else bear_ob,
            "FVG": fvg.get("label", "No FVG"),
            "Support": sr["support"],
            "Resistance": sr["resistance"],
            "Trend Strength": trend["label"],
            "Pullback Quality": pullback,
            "Price Action Score": pa_score,
            "Price Action Confidence %": confidence,
            "Decision": pa_decision if direction else "⏸️ WAIT",
            "Reject Reason": reject_reason if direction else "No clear directional confluence",
            "Institutional Grade": institutional_grade,
        }
    except Exception as e:
        return {
            "Swing Structure": "N/A", "BOS Status": "N/A", "CHOCH Status": "N/A",
            "Breakout Status": "NO", "Liquidity Sweep": "No Sweep", "Order Block": "No",
            "FVG": "No FVG", "Support": None, "Resistance": None,
            "Trend Strength": "N/A", "Pullback Quality": "N/A",
            "Price Action Score": 0, "Price Action Confidence %": 0.0,
            "Decision": "🚫 NO TRADE", "Reject Reason": f"Price-action error: {type(e).__name__}: {e}",
            "Institutional Grade": "REJECT",
        }


def _run_20_point_validation(*, htf_trend, smc_structure, cisd_signal, ob_meta, fvg, liquidity_sweep, volume_ok, last_close, prev_close, atr_val, vwap_val, ema20, ema50, rsi_val, macd_bullish, adx_val, rr, direction):
    """20-point institutional validation checklist. Logic unchanged."""
    is_buy = direction == "BUY"
    checks = [
        ("1. HTF Trend", ("Bullish" in htf_trend) if is_buy else ("Bearish" in htf_trend)),
        ("2. Market Structure", smc_structure in ("BOS 📈", "CHOCH 🐂") if is_buy else smc_structure in ("BOS 📉", "CHOCH 🐻")),
        ("3. BOS", "BOS" in smc_structure and ("📈" in smc_structure if is_buy else "📉" in smc_structure)),
        ("4. CHOCH", "CHOCH" in smc_structure),
        ("5. CISD", ("Bullish" in cisd_signal) if is_buy else ("Bearish" in cisd_signal)),
        ("6. OB Quality", ob_meta.get("bull_ob_type") not in ("Retail", "None") if is_buy else ob_meta.get("bear_ob_type") not in ("Retail", "None")),
        ("7. Fresh OB", ob_meta.get("bull_ob_fresh", False) if is_buy else ob_meta.get("bear_ob_fresh", False)),
        ("8. Untested OB", ob_meta.get("bull_ob_fresh", False) if is_buy else ob_meta.get("bear_ob_fresh", False)),
        ("9. Liquidity Sweep", ("Buy" in liquidity_sweep) if is_buy else ("Sell" in liquidity_sweep)),
        ("10. FVG", fvg.get("type") == ("Bullish" if is_buy else "Bearish") and not fvg.get("mitigated", True)),
        ("11. Volume", volume_ok),
        ("12. Candle", (last_close > prev_close) if is_buy else (last_close < prev_close)),
        ("13. ATR Move", abs(last_close - prev_close) >= 0.3 * atr_val if atr_val and atr_val > 0 else False),
        ("14. Momentum", rsi_val > 45 if is_buy else rsi_val < 55),
        ("15. VWAP", (last_close > vwap_val) if (is_buy and vwap_val) else ((last_close < vwap_val) if vwap_val else False)),
        ("16. EMA Trend", (ema20 > ema50) if is_buy else (ema20 < ema50)),
        ("17. RSI", (45 < rsi_val < 80) if is_buy else (20 < rsi_val < 55)),
        ("18. MACD", macd_bullish if is_buy else not macd_bullish),
        ("19. ADX > 20", adx_val >= 20),
        ("20. RR >= 1:2", rr >= 2.0),
    ]
    passed = [name for name, ok in checks if ok]
    failed = [name for name, ok in checks if not ok]
    return len(passed), passed, failed


def _signal_grade(passed_count: int, ai_confidence: float) -> str:
    if passed_count == 20 and ai_confidence >= 93:
        return "A+"
    if passed_count >= 17 and ai_confidence >= 88:
        return "A"
    if passed_count >= 14 and ai_confidence >= 80:
        return "B"
    if passed_count >= 10 and ai_confidence >= 70:
        return "C"
    return "REJECT"


def _enhanced_ai_confidence(passed_count, adx_val, rvol_raw, fvg, liquidity_swept, htf_aligned, ob_institutional) -> float:
    base = (passed_count / 20) * 70
    bonus = min(adx_val / 50 * 8, 8) + min((rvol_raw - 1) * 3, 6)
    bonus += 4 if not fvg.get("mitigated", True) else 0
    bonus += 4 if liquidity_swept else 0
    bonus += 4 if htf_aligned else 0
    bonus += 4 if ob_institutional else 0
    return round(max(0.0, min(100.0, base + bonus)), 1)


def _build_reason_list(direction, passed_list, fvg, liquidity_sweep, htf_trend, ob_meta) -> List[str]:
    is_buy = direction == "BUY"; reasons = []
    if "Bullish" in htf_trend or "Bearish" in htf_trend:
        reasons.append(f"HTF {'Uptrend ✓' if is_buy else 'Downtrend ✓'}")
    if any("BOS" in p for p in passed_list):
        reasons.append(f"{'Bullish' if is_buy else 'Bearish'} BOS Confirmed ✓")
    if any("CHOCH" in p for p in passed_list):
        reasons.append("CHOCH Confirmed ✓")
    if any("CISD" in p for p in passed_list):
        reasons.append(f"{'Bullish' if is_buy else 'Bearish'} CISD ✓")
    ob_label = ob_meta.get("bullish_ob_label" if is_buy else "bearish_ob_label", "")
    if "Fresh" in ob_label or "Institutional" in ob_label:
        reasons.append("Fresh Demand OB ✓" if is_buy else "Fresh Supply OB ✓")
    if fvg.get("type") and not fvg.get("mitigated"):
        reasons.append(f"{'Bullish' if is_buy else 'Bearish'} FVG (Unmitigated) ✓")
    if "Sweep" in liquidity_sweep:
        reasons.append(f"Liquidity Sweep ✓ {liquidity_sweep}")
    if any("Volume" in p for p in passed_list):
        reasons.append("Volume Expansion ✓")
    if any("VWAP" in p for p in passed_list):
        reasons.append(f"{'Above' if is_buy else 'Below'} VWAP ✓")
    if any("EMA" in p for p in passed_list):
        reasons.append("EMA Alignment ✓")
    if any("MACD" in p for p in passed_list):
        reasons.append(f"MACD {'Bullish' if is_buy else 'Bearish'} ✓")
    if any("RSI" in p for p in passed_list):
        reasons.append(f"RSI {'Bullish Zone' if is_buy else 'Bearish Zone'} ✓")
    if any("Momentum" in p for p in passed_list):
        reasons.append("Strong Momentum ✓")
    if any("RR" in p for p in passed_list):
        reasons.append("Risk:Reward ≥ 1:2 ✓")
    if ob_meta.get("bull_ob_institutional" if is_buy else "bear_ob_institutional", False):
        reasons.append("Institutional Buying ✓" if is_buy else "Institutional Selling ✓")
    return reasons


def _build_ai_report(direction, htf_trend, smc_structure, ob_meta, fvg, liquidity_sweep, volume_ok, vwap_val, last_close, ema20, ema50, momentum, adx_val, atr_val, ai_confidence, grade) -> str:
    is_buy = direction == "BUY"
    lines = [
        f"HTF Trend     : {htf_trend}",
        f"Structure     : {smc_structure}",
        f"OB            : {ob_meta.get('bullish_ob_label' if is_buy else 'bearish_ob_label', '—')}",
        f"FVG           : {fvg.get('label', 'No FVG')}",
        f"Liquidity     : {liquidity_sweep}",
        f"Volume        : {'High ✓' if volume_ok else 'Low ✗'}",
        f"VWAP          : {'Above ✓' if vwap_val and last_close > vwap_val else 'Below'}",
        f"EMA Trend     : {'EMA20>EMA50 ✓' if ema20 > ema50 else 'EMA20<EMA50'}",
        f"Momentum      : {momentum}",
        f"ADX           : {adx_val} ({'Trending ✓' if adx_val >= 25 else 'Sideways'})",
        f"ATR           : {round(atr_val, 2) if atr_val else '—'}",
        f"Risk          : {'Low' if grade in ('A+', 'A') else 'Medium' if grade == 'B' else 'High'}",
        f"Confidence    : {ai_confidence}%",
        f"Trade Quality : {'Institutional ✓' if ob_meta.get('bull_ob_institutional' if is_buy else 'bear_ob_institutional') else 'Retail'}",
    ]
    return " | ".join(lines)


ENHANCED_MIN_CONFIDENCE = 80.0
_PASSING_GRADES = {"A+", "A", "B", "C"}


def _analyse_enhanced(symbol, df, nifty_close, enable_xgboost) -> dict:
    """Calls existing _analyse() then appends all enhanced-engine columns,
    PLUS the full institutional Price Action engine report columns
    (Swing Structure, BOS/CHOCH Status, Breakout Status, Liquidity Sweep,
    Order Block, FVG, Support, Resistance, Trend Strength, Pullback
    Quality, Price Action Score/Confidence, Decision, Reject Reason,
    Institutional Grade). Nothing previously returned is removed."""
    base = _analyse(symbol, df, nifty_close, enable_xgboost)
    close = df["Close"]
    last_close = float(close.iloc[-1])
    prev_close = float(close.iloc[-2]) if len(close) >= 2 else last_close
    ema20 = float(close.ewm(span=20).mean().iloc[-1])
    ema50 = float(close.ewm(span=50).mean().iloc[-1])
    rsi_val = base["RSI"]
    macd_bullish = "Bullish" in base["MACD Signal"]
    vwap_val = base["VWAP"]
    atr_val = base.get("_ATR14") or last_close * 0.01
    rvol_raw = base.get("_RVOL_RAW", 0.0)
    vol_avg20 = float(df["Volume"].tail(20).mean())
    volume_ok = bool(vol_avg20 > 0 and float(df["Volume"].iloc[-1]) > vol_avg20)
    adx_val, plus_di, minus_di = calculate_adx(df)
    fvg = detect_fvg(df)
    liquidity_sweep, sweep_side = detect_liquidity_sweep(df)
    htf_trend = detect_htf_trend(df)
    momentum = calculate_momentum(df, rsi_val, macd_bullish, adx_val)
    smc_structure = base["SMC Structure"]; cisd_signal = base["CISD"]
    ob_meta = classify_order_block(df, smc_structure, fvg)
    quality_direction = "BUY" if base.get("AI Score", 50) >= 55 else "SELL"
    if quality_direction == "BUY":
        sl = round(last_close - 1.5 * atr_val, 2); t1 = round(last_close + 2.0 * atr_val, 2)
        t2 = round(last_close + 3.5 * atr_val, 2); t3 = round(last_close + 5.0 * atr_val, 2)
    else:
        sl = round(last_close + 1.5 * atr_val, 2); t1 = round(last_close - 2.0 * atr_val, 2)
        t2 = round(last_close - 3.5 * atr_val, 2); t3 = round(last_close - 5.0 * atr_val, 2)
    risk_amt = abs(last_close - sl); reward_amt = abs(t1 - last_close)
    rr = round(reward_amt / risk_amt, 2) if risk_amt > 0 else 0.0
    passed_count, passed_list, failed_list = _run_20_point_validation(
        htf_trend=htf_trend, smc_structure=smc_structure, cisd_signal=cisd_signal, ob_meta=ob_meta,
        fvg=fvg, liquidity_sweep=liquidity_sweep, volume_ok=volume_ok, last_close=last_close,
        prev_close=prev_close, atr_val=atr_val, vwap_val=vwap_val, ema20=ema20, ema50=ema50,
        rsi_val=rsi_val, macd_bullish=macd_bullish, adx_val=adx_val, rr=rr, direction=quality_direction)
    htf_aligned = ("Bullish" in htf_trend and quality_direction == "BUY") or ("Bearish" in htf_trend and quality_direction == "SELL")
    ob_institutional = ob_meta.get("bull_ob_institutional", False) if quality_direction == "BUY" else ob_meta.get("bear_ob_institutional", False)
    liquidity_swept = "Sweep" in liquidity_sweep
    ai_confidence = _enhanced_ai_confidence(passed_count, adx_val, rvol_raw, fvg, liquidity_swept, htf_aligned, ob_institutional)
    grade = _signal_grade(passed_count, ai_confidence)
    reasons = _build_reason_list(quality_direction, passed_list, fvg, liquidity_sweep, htf_trend, ob_meta)
    reason_str = " | ".join(reasons) if reasons else "—"
    ai_report = _build_ai_report(quality_direction, htf_trend, smc_structure, ob_meta, fvg, liquidity_sweep, volume_ok, vwap_val, last_close, ema20, ema50, momentum, adx_val, atr_val, ai_confidence, grade)
    if grade == "REJECT" or ai_confidence < ENHANCED_MIN_CONFIDENCE:
        enhanced_decision = "⬛ REJECT"; enhanced_signal = "⬛ Rejected"
    elif grade == "A+" and quality_direction == "BUY":
        enhanced_decision = "🟢🟢 Institutional BUY"; enhanced_signal = "🟢🟢 Strong BUY"
    elif grade == "A+" and quality_direction == "SELL":
        enhanced_decision = "🔴🔴 Institutional SELL"; enhanced_signal = "🔴🔴 Strong SELL"
    elif grade == "A" and quality_direction == "BUY":
        enhanced_decision = "🟢 BUY"; enhanced_signal = "🟢 BUY"
    elif grade == "A" and quality_direction == "SELL":
        enhanced_decision = "🔴 SELL"; enhanced_signal = "🔴 SELL"
    elif grade == "B" and quality_direction == "BUY":
        enhanced_decision = "🟢 BUY (B)"; enhanced_signal = "🟢 BUY"
    elif grade == "B" and quality_direction == "SELL":
        enhanced_decision = "🔴 SELL (B)"; enhanced_signal = "🔴 SELL"
    else:
        enhanced_decision = "🟠 WAIT"; enhanced_signal = "🟠 WAIT"
    enhanced_cols = {
        "HTF Trend": htf_trend, "ADX": adx_val, "+DI": plus_di, "-DI": minus_di, "Momentum": momentum,
        "FVG": fvg.get("label", "No FVG"), "FVG Gap Size": fvg.get("gap_size", 0.0),
        "FVG Filled %": fvg.get("filled_pct", 0.0), "FVG Age (Candles)": fvg.get("age_candles"),
        "FVG Freshness": fvg.get("freshness", "—"), "FVG Mitigated": "Yes" if fvg.get("mitigated") else "No",
        "FVG Nearest Distance": fvg.get("nearest_dist"),
        "OB Type (Bullish)": ob_meta.get("bullish_ob_label", "No Bullish OB"),
        "OB Type (Bearish)": ob_meta.get("bearish_ob_label", "No Bearish OB"),
        "Liquidity Sweep": liquidity_sweep, "Confirmations Passed": passed_count,
        "Confirmations Failed": ", ".join(failed_list) if failed_list else "None",
        "Signal Grade": grade, "AI Confidence %": ai_confidence,
        "Enhanced Entry": round(last_close, 2), "Enhanced SL": sl,
        "Enhanced Target 1": t1, "Enhanced Target 2": t2, "Enhanced Target 3": t3, "Enhanced RR": rr,
        "Signal Reason": reason_str, "AI Report": ai_report,
        "Enhanced Signal": enhanced_signal, "Enhanced Decision": enhanced_decision,
        "_Enhanced_Pass": grade in _PASSING_GRADES and ai_confidence >= ENHANCED_MIN_CONFIDENCE,
    }
    # Institutional Price Action engine columns (Swing Structure, BOS/CHOCH
    # Status, Price Action Score/Confidence, Decision, Reject Reason,
    # Institutional Grade, etc.) are computed exactly once inside
    # `_analyse()` and already flow through via `base` here — this avoids
    # recomputing the whole Price Action engine a second time per symbol
    # (duplicate-calculation / CPU-usage requirement from the fix spec).
    return {**base, **enhanced_cols}


def _fetch_symbol_enhanced(fyers, symbol, nifty_close, enable_xgboost):
    """Per-symbol worker for the Institutional Scanner tab. Logic unchanged."""
    if not isinstance(symbol, str) or not _VALID_EQ_SYMBOL_RE.match(symbol):
        return None, f"{symbol}: invalid symbol format — skipped"
    resp, err = _safe_history(fyers, {"symbol": symbol, "resolution": "D", "date_format": "1", "range_from": DATE_FROM, "range_to": DATE_TO, "cont_flag": "1"})
    if err:
        return None, f"{symbol}: {err}"
    candles = resp.get("candles") if resp else None
    if not candles or len(candles) < 30:
        return None, f"{symbol}: insufficient history"
    try:
        df = pd.DataFrame(candles, columns=["Time", "Open", "High", "Low", "Close", "Volume"])
        df["Time"] = pd.to_datetime(df["Time"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
        df[["Open", "High", "Low", "Close", "Volume"]] = df[["Open", "High", "Low", "Close", "Volume"]].apply(pd.to_numeric, errors="coerce")
        df = df.dropna(subset=["Open", "High", "Low", "Close"])
        if len(df) < 30:
            return None, f"{symbol}: insufficient valid candle data"
    except (KeyError, ValueError, TypeError) as e:
        return None, f"{symbol}: malformed candle data ({e})"
    try:
        return _analyse_enhanced(symbol, df, nifty_close, enable_xgboost), None
    except Exception as e:
        return None, f"{symbol}: enhanced analysis error ({type(e).__name__})"


def run_scan_enhanced(fyers, symbols, nifty_close, enable_xgboost):
    """Threaded batch scan for the Institutional Scanner tab. Logic unchanged."""
    symbols = _validate_symbols(symbols)
    results, errors = [], []
    stats = ScanStats(total=len(symbols))
    progress = st.progress(0.0, text=f"Institutional Scan 0 / {len(symbols)}")
    done = 0
    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i:i + BATCH_SIZE]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(_fetch_symbol_enhanced, fyers, s, nifty_close, enable_xgboost): s for s in batch}
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
                progress.progress(done / max(len(symbols), 1), text=f"Institutional Scan {done} / {len(symbols)}")
        if i + BATCH_SIZE < len(symbols):
            time.sleep(BATCH_PAUSE_SECONDS)
    progress.empty()
    gc.collect()
    return results, errors, stats


def _analyse(symbol, df, nifty_close, enable_xgboost) -> dict:
    """Core per-symbol daily analysis used by the Full/F&O scanners.
    Original indicator logic unchanged; now also merges in the full
    institutional Price Action engine report columns (computed exactly
    once here so `_analyse_enhanced` can reuse them without recomputation)."""
    close, volume = df["Close"], df["Volume"]
    ema20 = close.ewm(span=20).mean().iloc[-1]
    ema50 = close.ewm(span=50).mean().iloc[-1]
    ema200 = close.ewm(span=200).mean().iloc[-1] if len(close) >= 200 else close.ewm(span=len(close)).mean().iloc[-1]
    vol_avg20 = volume.tail(20).mean()
    rvol = (volume.iloc[-1] / vol_avg20) if vol_avg20 > 0 else 0
    trend_score = sum([close.iloc[-1] > ema20, close.iloc[-1] > ema50, close.iloc[-1] > ema200]) / 3
    roc = (close.iloc[-1] / close.iloc[-10] - 1) * 100 if len(close) >= 10 else 0
    ai_score = min(round((rvol * 15) + (trend_score * 40) + min(max(roc, 0), 10) * 2 + 20, 1), 100)
    gap_pct = 0.0
    if len(df) >= 2 and df["Close"].iloc[-2] not in (0, None) and pd.notna(df["Close"].iloc[-2]):
        gap_pct = ((df["Open"].iloc[-1] - df["Close"].iloc[-2]) / df["Close"].iloc[-2]) * 100
    gap_str = f"{gap_pct:.2f}%"
    if gap_pct >= 0.5:
        gap_str += " 🟢"
    elif gap_pct <= -0.5:
        gap_str += " 🔴"
    smc_structure, cisd_signal, _signal_event_ts = _calculate_smc_and_cisd(df)
    bullish_ob, bearish_ob, ob_zone, ob_strength = _detect_order_blocks(df, smc_structure)
    h52w = df["High"].max(); l52w = df["Low"].min(); last_close = close.iloc[-1]
    if pd.notna(h52w) and last_close >= h52w * 0.97:
        status_52w = "🟢 Near High"
    elif pd.notna(l52w) and last_close <= l52w * 1.03:
        status_52w = "🔴 Near Low"
    else:
        status_52w = "Mid Range"
    resistance = df["High"].rolling(20).max().shift(1).iloc[-1]
    support = df["Low"].rolling(20).min().shift(1).iloc[-1]
    if pd.notna(resistance) and last_close > resistance:
        breakout = "📈 Bullish"
    elif pd.notna(support) and last_close < support:
        breakout = "📉 Bearish"
    else:
        breakout = "NO"
    rsi_val = round(float(calculate_rsi(close).iloc[-1]), 1)
    macd_line, signal_line, macd_hist = calculate_macd(close)
    macd_bullish = bool(macd_line.iloc[-1] > signal_line.iloc[-1])
    macd_signal_str = "🟢 Bullish Crossover" if macd_bullish else "🔴 Bearish Crossover"
    supertrend_label, supertrend_bullish, _ = calculate_supertrend(df)
    vwap_val = calculate_vwap_approx(df)
    chart_pattern = detect_chart_pattern(df)
    mtf_trend = calculate_mtf_trend(df)
    rs_label = calculate_relative_strength(close, nifty_close)
    atr14 = calculate_atr(df).iloc[-1]
    direction = "Bullish" if breakout == "📈 Bullish" or macd_bullish else ("Bearish" if breakout == "📉 Bearish" or not macd_bullish else "Neutral")
    target, stoploss = calculate_target_stoploss(last_close, atr14, direction)
    ai_trend, ai_confidence = calculate_ai_trend(ai_score)
    xgb_trend, xgb_confidence = calculate_xgboost_prediction(df, rsi_val=rsi_val, macd_bullish=macd_bullish, supertrend_bullish=supertrend_bullish, vwap_val=vwap_val, rvol=rvol, support=support, resistance=resistance, use_ml=enable_xgboost)
    alerts = generate_alerts(rvol, breakout, cisd_signal, mtf_trend, gap_pct)
    final_signal = calculate_final_signal(ai_score=ai_score, xgb_trend=xgb_trend, mtf_trend=mtf_trend, rs_label=rs_label, rsi=rsi_val, macd_bullish=macd_bullish, supertrend_bullish=supertrend_bullish, breakout=breakout, cisd_signal=cisd_signal, smc_structure=smc_structure)
    stock_ticker = symbol.replace("NSE:", "").replace("-EQ", "")
    news = calculate_news(stock_ticker, gap_pct, rvol, breakout)
    rvol_raw = round(float(rvol), 2)
    rvol_display = _format_rvol_display(rvol_raw)
    quality_direction, quality_count, is_high_quality, signal_strength, signal_reason = _calculate_signal_quality(ema20=float(ema20), ema50=float(ema50), rsi_val=rsi_val, macd_bullish=macd_bullish, supertrend_bullish=supertrend_bullish, vwap_val=vwap_val, last_close=float(last_close), rvol_raw=rvol_raw, breakout=breakout, cisd_signal=cisd_signal, smc_structure=smc_structure, last_volume=float(volume.iloc[-1]), vol_avg20=float(vol_avg20))
    entry_confirmation, trade_quality, trade_decision = _determine_entry_and_decision(direction=quality_direction, confirmed_count=quality_count, ai_score=ai_score, confidence=xgb_confidence, rvol_raw=rvol_raw, volume_ok=bool(vol_avg20 and vol_avg20 > 0 and float(volume.iloc[-1]) > vol_avg20))
    if _signal_event_ts is not None:
        signal_date_str, signal_time_str = _format_signal_timestamp(_signal_event_ts, is_daily=True)
    else:
        signal_date_str, signal_time_str = _candle_signal_timestamp(df, is_daily=True)

    base_row = {
        "Signal Date": signal_date_str, "Signal Time": signal_time_str, "Stock": stock_ticker, "LTP": round(last_close, 2), "Gap %": gap_str, "Target": target, "Stoploss": stoploss,
        "SMC Structure": smc_structure, "CISD": cisd_signal, "Bullish Order Block": bullish_ob, "Bearish Order Block": bearish_ob, "Order Block Zone": ob_zone, "Order Block Strength": ob_strength,
        "XGBoost Trend": xgb_trend, "XGBoost Confidence (%)": xgb_confidence, "News": news, "Alerts": alerts, "Signal Strength": signal_strength, "Entry Confirmation": entry_confirmation,
        "Signal Reason": signal_reason, "Trade Quality": trade_quality, "Trade Decision": trade_decision, "MTF Trend": mtf_trend, "AI Trend": ai_trend, "AI Confidence (%)": ai_confidence,
        "RS vs NIFTY": rs_label, "Support": round(float(support), 2) if pd.notna(support) else None, "Resistance": round(float(resistance), 2) if pd.notna(resistance) else None,
        "52W High": round(float(h52w), 2) if pd.notna(h52w) else None, "52W Low": round(float(l52w), 2) if pd.notna(l52w) else None, "52W Status": status_52w,
        "RSI": rsi_val, "Breakout Status": breakout, "MACD Signal": macd_signal_str, "Supertrend": supertrend_label, "VWAP": vwap_val, "Chart Pattern": chart_pattern, "RVOL": rvol_display,
        "AI Score": ai_score, "Final Signal": final_signal, "Smart Money": "🏦 Institutional" if ai_score > 70 else "⚖️ Neutral" if ai_score > 45 else "🔻 Distribution",
        "Signal": "🟢 BUY" if ai_score > 65 else "🔴 SELL" if ai_score < 40 else "🟡 HOLD",
        "_ATR14": round(float(atr14), 2) if pd.notna(atr14) else round(last_close * 0.01, 2), "_RVOL_RAW": rvol_raw, "_Is_High_Quality": is_high_quality, "_Quality_Count": quality_count,
    }

    # ── Institutional Price Action engine — additive report columns ──────
    # Computed exactly once per symbol here (both _analyse and the
    # Institutional Scanner's _analyse_enhanced reuse this same dict, so
    # the whole Price Action engine never runs twice for one candle set).
    # "Support"/"Resistance"/"Breakout Status" already exist above with
    # different (simpler, rolling-window) semantics, so the new
    # institutional versions are exposed under distinct "PA "-prefixed
    # keys — nothing existing is overwritten or removed.
    pa_cols_raw = build_price_action_report_row(df, symbol)
    pa_collision_keys = {"Support", "Resistance", "Breakout Status"}
    pa_cols = {(f"PA {k}" if k in pa_collision_keys else k): v for k, v in pa_cols_raw.items()}

    return {**base_row, **pa_cols}


def calculate_intraday_signal(row) -> dict:
    """Derive the 'Intraday Scanner' signal from an already-computed daily analysis row. Logic unchanged."""
    try:
        last_close = row["LTP"]; atr = row.get("_ATR14") or round(last_close * 0.01, 2)
        rsi = row["RSI"]; macd_bullish = "Bullish" in row["MACD Signal"]
        supertrend_label = row["Supertrend"]; vwap = row["VWAP"]
        rvol = row.get("_RVOL_RAW", 0.0); breakout = row["Breakout Status"]; ai_score = row["AI Score"]
        bull_votes = sum([macd_bullish, "Buy" in supertrend_label, vwap is not None and last_close > vwap, rsi > 50, breakout == "📈 Bullish"])
        bear_votes = sum([not macd_bullish, "Sell" in supertrend_label, vwap is not None and last_close < vwap, rsi < 50, breakout == "📉 Bearish"])
        if bull_votes >= 4 and rvol >= 1.2:
            signal = "🟢 BUY"
        elif bear_votes >= 4 and rvol >= 1.2:
            signal = "🔴 SELL"
        else:
            signal = "🟡 WAIT"
        entry = round(last_close, 2)
        if signal == "🟢 BUY":
            sl = round(entry - 1.0 * atr, 2); t1 = round(entry + 1.0 * atr, 2); t2 = round(entry + 1.8 * atr, 2); t3 = round(entry + 2.6 * atr, 2); exit_cond = "Exit if price closes below SL or Supertrend flips Sell"
        elif signal == "🔴 SELL":
            sl = round(entry + 1.0 * atr, 2); t1 = round(entry - 1.0 * atr, 2); t2 = round(entry - 1.8 * atr, 2); t3 = round(entry - 2.6 * atr, 2); exit_cond = "Exit if price closes above SL or Supertrend flips Buy"
        else:
            sl = round(entry - 1.0 * atr, 2); t1 = t2 = t3 = entry; exit_cond = "No trade — wait for alignment"
        risk = abs(entry - sl); reward = abs(t1 - entry)
        rr_ratio = round(reward / risk, 2) if risk > 0 else 0.0
        vote_total = max(bull_votes, bear_votes)
        confidence = max(30.0, round(min(95.0, 40 + vote_total * 11 + min(rvol, 3) * 5), 1))
        atr_pct = (atr / last_close * 100) if last_close else 0
        if atr_pct >= 3:
            holding_time = "15–45 Minutes (high volatility)"
        elif atr_pct >= 1.5:
            holding_time = "30–90 Minutes"
        else:
            holding_time = "1–3 Hours"
        reasons = ["MACD bullish" if macd_bullish else "MACD bearish", f"Supertrend {supertrend_label.split()[-1]}"]
        if vwap is not None:
            reasons.append("Above VWAP" if last_close > vwap else "Below VWAP")
        reasons.append(f"RSI {rsi}")
        if rvol >= 1.5:
            reasons.append(f"High RVOL {rvol}x")
        if breakout != "NO":
            reasons.append(f"Breakout: {breakout}")
        return {"Signal Date": row["Signal Date"], "Signal Time": row["Signal Time"], "Stock": row["Stock"], "LTP": last_close, "Intraday Signal": signal, "Entry Price": entry, "Stop Loss": sl, "Target 1": t1, "Target 2": t2, "Target 3": t3, "Risk Reward Ratio": rr_ratio, "Confidence %": confidence, "AI Score": ai_score, "Bullish Order Block": row.get("Bullish Order Block", "No"), "Bearish Order Block": row.get("Bearish Order Block", "No"), "Order Block Zone": row.get("Order Block Zone", "—"), "Order Block Strength": row.get("Order Block Strength", "—"), "Expected Holding Time": holding_time, "Exit Condition": exit_cond, "Reason": ", ".join(reasons)}
    except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError, AttributeError):
        return {"Signal Date": row.get("Signal Date", "N/A"), "Signal Time": row.get("Signal Time", "N/A"), "Stock": row.get("Stock", "N/A"), "LTP": row.get("LTP"), "Intraday Signal": "🟡 WAIT", "Entry Price": row.get("LTP"), "Stop Loss": None, "Target 1": None, "Target 2": None, "Target 3": None, "Risk Reward Ratio": 0.0, "Confidence %": 0.0, "AI Score": row.get("AI Score", 0), "Bullish Order Block": row.get("Bullish Order Block", "No"), "Bearish Order Block": row.get("Bearish Order Block", "No"), "Order Block Zone": row.get("Order Block Zone", "—"), "Order Block Strength": row.get("Order Block Strength", "—"), "Expected Holding Time": "N/A", "Exit Condition": "Insufficient data", "Reason": "Insufficient data"}


def calculate_swing_signal(row) -> dict:
    """Derive the 'Swing Trade Scanner' signal from an already-computed daily analysis row. Logic unchanged."""
    try:
        last_close = row["LTP"]; atr = row.get("_ATR14") or round(last_close * 0.01, 2)
        mtf_trend = row["MTF Trend"]; rs_label = row["RS vs NIFTY"]
        supertrend_label = row["Supertrend"]; smc_structure = row["SMC Structure"]
        cisd_signal = row["CISD"]; ai_score = row["AI Score"]
        bull_votes = sum(["Aligned Bullish" in mtf_trend, "Outperform" in rs_label, "Buy" in supertrend_label, "📈" in smc_structure or "🐂" in smc_structure, "Bullish" in cisd_signal])
        bear_votes = sum(["Aligned Bearish" in mtf_trend, "Underperform" in rs_label, "Sell" in supertrend_label, "📉" in smc_structure or "🐻" in smc_structure, "Bearish" in cisd_signal])
        if bull_votes >= 3:
            signal = "🟢 BUY"
        elif bear_votes >= 3:
            signal = "🔴 SELL"
        else:
            signal = "🟡 HOLD"
        entry = round(last_close, 2)
        if signal == "🟢 BUY":
            sl = round(entry - 2.0 * atr, 2); t1 = round(entry + 2.0 * atr, 2); t2 = round(entry + 3.5 * atr, 2); t3 = round(entry + 5.0 * atr, 2); exit_cond = "Exit on daily close below SL or MTF turns Bearish"
        elif signal == "🔴 SELL":
            sl = round(entry + 2.0 * atr, 2); t1 = round(entry - 2.0 * atr, 2); t2 = round(entry - 3.5 * atr, 2); t3 = round(entry - 5.0 * atr, 2); exit_cond = "Exit on daily close above SL or MTF turns Bullish"
        else:
            sl = round(entry - 2.0 * atr, 2); t1 = t2 = t3 = entry; exit_cond = "No position — wait for alignment"
        risk = abs(entry - sl); reward = abs(t1 - entry)
        rr_ratio = round(reward / risk, 2) if risk > 0 else 0.0
        vote_total = max(bull_votes, bear_votes)
        confidence = max(30.0, round(min(95.0, 38 + vote_total * 12 + (ai_score - 50) * 0.15), 1))
        trend_strength = "🟢 Strong" if vote_total >= 4 else ("🟡 Moderate" if vote_total == 3 else "🔴 Weak")
        atr_pct = (atr / last_close * 100) if last_close else 0
        if atr_pct >= 3:
            holding_days, est_days = "3–7 Days", 5
        elif atr_pct >= 1.5:
            holding_days, est_days = "7–14 Days", 10
        else:
            holding_days, est_days = "14–25 Days", 18
        exit_date = (_now_ist() + timedelta(days=est_days)).strftime("%d-%b-%Y")
        reasons = [f"MTF: {mtf_trend}", f"RS vs NIFTY: {rs_label}", f"Supertrend: {supertrend_label}", f"SMC: {smc_structure}"]
        if cisd_signal != "None":
            reasons.append(f"CISD: {cisd_signal}")
        return {"Signal Date": row["Signal Date"], "Signal Time": row["Signal Time"], "Stock": row["Stock"], "Swing Signal": signal, "Swing Entry": entry, "Swing Stop Loss": sl, "Swing Target 1": t1, "Swing Target 2": t2, "Swing Target 3": t3, "Expected Holding Period": holding_days, "Estimated Exit Date": exit_date, "Exit Condition": exit_cond, "Trend Strength": trend_strength, "Confidence %": confidence, "AI Score": ai_score, "Risk Reward Ratio": rr_ratio, "Bullish Order Block": row.get("Bullish Order Block", "No"), "Bearish Order Block": row.get("Bearish Order Block", "No"), "Order Block Zone": row.get("Order Block Zone", "—"), "Order Block Strength": row.get("Order Block Strength", "—"), "Reason": ", ".join(reasons)}
    except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError, AttributeError):
        return {"Signal Date": row.get("Signal Date", "N/A"), "Signal Time": row.get("Signal Time", "N/A"), "Stock": row.get("Stock", "N/A"), "Swing Signal": "🟡 HOLD", "Swing Entry": row.get("LTP"), "Swing Stop Loss": None, "Swing Target 1": None, "Swing Target 2": None, "Swing Target 3": None, "Expected Holding Period": "N/A", "Estimated Exit Date": "N/A", "Exit Condition": "Insufficient data", "Trend Strength": "🔴 Weak", "Confidence %": 0.0, "AI Score": row.get("AI Score", 0), "Risk Reward Ratio": 0.0, "Bullish Order Block": row.get("Bullish Order Block", "No"), "Bearish Order Block": row.get("Bearish Order Block", "No"), "Order Block Zone": row.get("Order Block Zone", "—"), "Order Block Strength": row.get("Order Block Strength", "—"), "Reason": "Insufficient data"}


def _fetch_symbol(fyers, symbol, nifty_close, enable_xgboost):
    """Per-symbol worker for the Full/F&O Scanner tabs. Logic unchanged."""
    if not isinstance(symbol, str) or not _VALID_EQ_SYMBOL_RE.match(symbol):
        return None, f"{symbol}: invalid symbol format — skipped"
    resp, err = _safe_history(fyers, {"symbol": symbol, "resolution": "D", "date_format": "1", "range_from": DATE_FROM, "range_to": DATE_TO, "cont_flag": "1"})
    if err:
        return None, f"{symbol}: {err}"
    candles = resp.get("candles") if resp else None
    if not candles or len(candles) < 30:
        return None, f"{symbol}: insufficient history ({len(candles) if candles else 0} candles)"
    try:
        df = pd.DataFrame(candles, columns=["Time", "Open", "High", "Low", "Close", "Volume"])
        df["Time"] = pd.to_datetime(df["Time"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
        df[["Open", "High", "Low", "Close", "Volume"]] = df[["Open", "High", "Low", "Close", "Volume"]].apply(pd.to_numeric, errors="coerce")
        df = df.dropna(subset=["Open", "High", "Low", "Close"])
        if len(df) < 30:
            return None, f"{symbol}: insufficient valid candle data after cleaning"
    except (KeyError, ValueError, TypeError) as e:
        return None, f"{symbol}: malformed candle data ({e})"
    try:
        return _analyse(symbol, df, nifty_close, enable_xgboost), None
    except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError, AttributeError) as e:
        return None, f"{symbol}: analysis error ({type(e).__name__})"
    except Exception as e:
        return None, f"{symbol}: unexpected error ({type(e).__name__})"


def run_scan(fyers, symbols, nifty_close, enable_xgboost):
    """Threaded batch scan for the Full/F&O Scanner tabs. Logic unchanged."""
    symbols = _validate_symbols(symbols)
    results, errors = [], []
    stats = ScanStats(total=len(symbols))
    progress = st.progress(0.0, text=f"Scanning 0 / {len(symbols)}")
    done = 0
    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i:i + BATCH_SIZE]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(_fetch_symbol, fyers, s, nifty_close, enable_xgboost): s for s in batch}
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
                progress.progress(done / len(symbols), text=f"Scanning {done} / {len(symbols)}")
        if i + BATCH_SIZE < len(symbols):
            time.sleep(BATCH_PAUSE_SECONDS)
    progress.empty()
    gc.collect()
    return results, errors, stats


def _color_code(val) -> str:
    """Cell-level colour coding for signal-bearing string columns."""
    if isinstance(val, str):
        if any(x in val for x in ["Strong Buy", "BUY", "Institutional", "🟢", "🔵", "Buy", "BOS 📈", "CHOCH 🐂", "Bullish", "Aligned Bullish", "Outperform", "Near High", "Bullish Engulfing", "Hammer", "Higher Highs", "📈", "Up", "Golden Cross"]):
            return "color: green; font-weight: bold;"
        if any(x in val for x in ["Strong Sell", "SELL", "Sell", "Distribution", "🔴", "🟠", "BOS 📉", "CHOCH 🐻", "Bearish", "Aligned Bearish", "Underperform", "Near Low", "Bearish Engulfing", "Shooting Star", "Lower Highs", "📉", "Down", "Death Cross", "REJECT"]):
            return "color: red; font-weight: bold;"
        if any(x in val for x in ["🟡", "Wait", "HOLD", "Neutral", "Mixed", "Inline", "WAIT", "WATCH"]):
            return "color: #b8860b; font-weight: bold;"
        if any(x in val for x in ["⬛", "Rejected"]):
            return "color: #888888; font-weight: bold;"
    return ""


def _style_dataframe(df):
    """Apply colour coding safely. Falls back to unstyled df on any error."""
    try:
        str_cols = [c for c in df.columns if df[c].dtype == object]
        if not str_cols:
            return df.style
        styler = df.style
        if hasattr(styler, "map"):
            return styler.map(_color_code, subset=str_cols)
        return styler.applymap(_color_code, subset=str_cols)
    except Exception:
        try:
            return df.style
        except Exception:
            return df


_SIGNAL_FILL_RULES = [
    ("STRONG BUY", "006100", "FFFFFF", True), ("STRONG SELL", "9C0006", "FFFFFF", True),
    ("WATCHLIST", "FFA500", "000000", True), ("BUY", "92D050", "000000", True),
    ("SELL", "FF0000", "FFFFFF", True), ("WAIT", "FFFF00", "000000", True),
    ("HOLD", "FFFF00", "000000", True), ("WATCH", "FFFF00", "000000", True),
    ("CISD UP", "92D050", "000000", True), ("CISD DOWN", "FF0000", "FFFFFF", True),
    ("REJECT", "888888", "FFFFFF", True),
]
_SUPPORT_FILL_HEX = "E2EFDA"; _RESISTANCE_FILL_HEX = "FCE4D6"
_HIGH_AI_SCORE_FILL_HEX = "7030A0"; _HIGH_RVOL_FILL_HEX = "00FFFF"
_HEADER_FILL_HEX = "1F4E78"; _BAND_FILL_HEX = "F2F2F2"


def _get_conditional_fill_font(col_name, value):
    """Resolve the openpyxl fill/font for a single cell based on its column & value."""
    from openpyxl.styles import Font, PatternFill
    text = "" if value is None else str(value)
    text_upper = text.upper()
    for keyword, fill_hex, font_hex, bold in _SIGNAL_FILL_RULES:
        if keyword in text_upper:
            return PatternFill("solid", fgColor=fill_hex), Font(color=font_hex, bold=bold)
    if col_name == "Support":
        return PatternFill("solid", fgColor=_SUPPORT_FILL_HEX), None
    if col_name == "Resistance":
        return PatternFill("solid", fgColor=_RESISTANCE_FILL_HEX), None
    if "RVOL" in col_name and ("❤️" in text or "🔥" in text):
        return PatternFill("solid", fgColor=_HIGH_RVOL_FILL_HEX), Font(bold=True)
    if col_name == "AI Score":
        try:
            if float(value) > 90:
                return PatternFill("solid", fgColor=_HIGH_AI_SCORE_FILL_HEX), Font(color="FFFFFF", bold=True)
        except (TypeError, ValueError):
            pass
    return None, None


def _format_worksheet(ws, df) -> None:
    """Apply the standard professional worksheet formatting used across all export tabs."""
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    thin = Side(style="thin", color="B0B0B0"); border = Border(left=thin, right=thin, top=thin, bottom=thin)
    center = Alignment(horizontal="center", vertical="center", wrap_text=False)
    header_font = Font(bold=True, color="FFFFFF", name="Arial", size=11)
    header_fill = PatternFill("solid", fgColor=_HEADER_FILL_HEX)
    for cell in ws[1]:
        cell.font = header_font; cell.fill = header_fill; cell.alignment = center; cell.border = border
    columns = list(df.columns); band_fill = PatternFill("solid", fgColor=_BAND_FILL_HEX)
    for r in range(2, ws.max_row + 1):
        row_is_band = (r % 2 == 0)
        for c in range(1, ws.max_column + 1):
            cell = ws.cell(row=r, column=c); cell.alignment = center; cell.border = border
            col_name = columns[c - 1] if c - 1 < len(columns) else ""
            fill, font = _get_conditional_fill_font(col_name, cell.value)
            if fill is not None:
                cell.fill = fill; cell.font = font if font else cell.font
            elif row_is_band:
                cell.fill = band_fill
    for col_cells in ws.columns:
        length = max((len(str(c.value)) for c in col_cells if c.value is not None), default=10)
        ws.column_dimensions[col_cells[0].column_letter].width = max(length + 2, 10)
    ws.freeze_panes = "A2"; ws.auto_filter.ref = ws.dimensions


def to_excel_bytes(df, sheet_name: str = "Scan Results") -> bytes:
    """Generic single-sheet Excel exporter used by every existing tab."""
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        safe_name = sheet_name[:31]; df.to_excel(writer, index=False, sheet_name=safe_name)
        _format_worksheet(writer.sheets[safe_name], df)
    buf.seek(0); return buf.getvalue()


def to_excel_bytes_multi(sheets: Dict[str, pd.DataFrame]) -> bytes:
    """Multi-sheet Excel exporter."""
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        for sheet_name, df in sheets.items():
            if df is None or df.empty:
                continue
            safe_name = sheet_name[:31]; df.to_excel(writer, index=False, sheet_name=safe_name)
            _format_worksheet(writer.sheets[safe_name], df)
    buf.seek(0); return buf.getvalue()


def to_csv_bytes(df) -> bytes:
    return df.to_csv(index=False).encode("utf-8")


def to_json_bytes(df) -> bytes:
    return df.to_json(orient="records", indent=2, force_ascii=False).encode("utf-8")


_INTRADAY_RESOLUTION_MAP = {"5 Minutes": "5", "15 Minutes": "15"}


def _is_intraday_candle_closed(candle_time_ist, resolution_minutes: int) -> bool:
    return _now_ist() >= candle_time_ist + timedelta(minutes=resolution_minutes)


def _fetch_intraday_cisd_signal(fyers, symbol, resolution, timeframe_label):
    """Per-symbol worker for the 'Intraday CISD Signals' tab. Logic unchanged."""
    if not isinstance(symbol, str) or not _VALID_EQ_SYMBOL_RE.match(symbol):
        return None, f"{symbol}: invalid symbol format — skipped"
    date_from = (datetime.today() - timedelta(days=INTRADAY_CISD_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    date_to = datetime.today().strftime("%Y-%m-%d")
    resp, err = _safe_history(fyers, {"symbol": symbol, "resolution": resolution, "date_format": "1", "range_from": date_from, "range_to": date_to, "cont_flag": "1"})
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
        if len(df) > 0 and not _is_intraday_candle_closed(df["Time"].iloc[-1], int(resolution)):
            df = df.iloc[:-1].reset_index(drop=True)
        if len(df) < 30:
            return None, None
        smc_structure, cisd_signal, event_ts = _calculate_smc_and_cisd(df)
        if cisd_signal == "None":
            return None, None
        last_close = float(df["Close"].iloc[-1]); atr = float(calculate_atr(df).iloc[-1])
        if pd.isna(atr) or atr <= 0:
            atr = last_close * 0.005
        is_up = "Bullish" in cisd_signal
        entry = round(last_close, 2)
        sl = round(entry - 1.0 * atr, 2) if is_up else round(entry + 1.0 * atr, 2)
        target = round(entry + 2.0 * atr, 2) if is_up else round(entry - 2.0 * atr, 2)
        risk = abs(entry - sl); reward = abs(target - entry)
        rr_ratio = round(reward / risk, 2) if risk > 0 else 0.0
        rsi_val = round(float(calculate_rsi(df["Close"]).iloc[-1]), 1)
        vol_avg20 = df["Volume"].tail(20).mean()
        rvol_raw = round(float(df["Volume"].iloc[-1] / vol_avg20), 2) if vol_avg20 > 0 else 0.0
        ai_score = round(min(max(50 + (rvol_raw * 10) + (10 if is_up else -10) + (rsi_val - 50) * 0.3, 0), 100), 1)
        confidence = round(min(95.0, max(35.0, 55 + min(rvol_raw, 3) * 8 + rr_ratio * 3)), 1)
        stock_ticker = symbol.replace("NSE:", "").replace("-EQ", "")
        signal_date_str, signal_time_str = (_format_signal_timestamp(event_ts) if event_ts is not None else _candle_signal_timestamp(df))
        return {"Signal Date": signal_date_str, "Signal Time": signal_time_str, "Timeframe": timeframe_label, "Stock": stock_ticker, "Signal": "🟢 ▲ CISD UP Signal" if is_up else "🔴 ▼ CISD DOWN Signal", "Entry": entry, "Stoploss": sl, "Target": target, "Confidence %": confidence, "AI Score": ai_score, "News": calculate_news(stock_ticker, 0.0, rvol_raw, "📈 Bullish" if is_up else "📉 Bearish"), "Reason": f"{timeframe_label} CISD {'bullish' if is_up else 'bearish'} shift confirmed (RSI {rsi_val}, RVOL {_format_rvol_display(rvol_raw)})"}, None
    except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError, AttributeError) as e:
        return None, f"{symbol}: analysis error ({type(e).__name__})"


def run_intraday_cisd_scan(fyers, symbols, resolution, timeframe_label):
    """Threaded batch scan for the 'Intraday CISD Signals' tab. Logic unchanged."""
    symbols = _validate_symbols(symbols); results, errors = [], []
    stats = ScanStats(total=len(symbols)); progress = st.progress(0.0, text=f"Scanning Intraday CISD 0 / {len(symbols)}"); done = 0
    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i:i + BATCH_SIZE]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(_fetch_intraday_cisd_signal, fyers, s, resolution, timeframe_label): s for s in batch}
            for future in as_completed(futures):
                try:
                    res, err = future.result()
                except Exception as e:
                    res, err = None, f"{futures[future]}: worker error ({type(e).__name__})"
                if res:
                    results.append(res)
                if err:
                    errors.append(err)
                stats.record(has_result=bool(res), has_error=bool(err)); done += 1
                progress.progress(done / len(symbols), text=f"Scanning Intraday CISD {done} / {len(symbols)}")
        if i + BATCH_SIZE < len(symbols):
            time.sleep(BATCH_PAUSE_SECONDS)
    progress.empty(); gc.collect()
    return results, errors, stats


def _fetch_fo_cisd_signal(fyers, symbol):
    """Per-symbol worker for the 'F&O CISD Scanner' tab. Logic unchanged."""
    if not isinstance(symbol, str) or not _VALID_EQ_SYMBOL_RE.match(symbol):
        return None, f"{symbol}: invalid symbol format — skipped"
    resp, err = _safe_history(fyers, {"symbol": symbol, "resolution": "D", "date_format": "1", "range_from": DATE_FROM, "range_to": DATE_TO, "cont_flag": "1"})
    if err:
        return None, f"{symbol}: {err}"
    candles = resp.get("candles") if resp else None
    if not candles or len(candles) < 30:
        return None, f"{symbol}: insufficient history"
    try:
        df = pd.DataFrame(candles, columns=["Time", "Open", "High", "Low", "Close", "Volume"])
        df["Time"] = pd.to_datetime(df["Time"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
        df[["Open", "High", "Low", "Close", "Volume"]] = df[["Open", "High", "Low", "Close", "Volume"]].apply(pd.to_numeric, errors="coerce")
        df = df.dropna(subset=["Open", "High", "Low", "Close"])
        if len(df) < 30:
            return None, f"{symbol}: insufficient valid data"
        smc_structure, cisd_signal, event_ts = _calculate_smc_and_cisd(df)
        if cisd_signal == "None":
            return None, None
        last_close = float(df["Close"].iloc[-1]); atr = float(calculate_atr(df).iloc[-1])
        if pd.isna(atr) or atr <= 0:
            atr = last_close * 0.01
        is_bull = "Bullish" in cisd_signal
        entry = round(last_close, 2)
        sl = round(entry - 1.5 * atr, 2) if is_bull else round(entry + 1.5 * atr, 2)
        target = round(entry + 3.0 * atr, 2) if is_bull else round(entry - 3.0 * atr, 2)
        risk = abs(entry - sl); reward = abs(target - entry); rr_ratio = round(reward / risk, 2) if risk > 0 else 0.0
        supertrend_label, supertrend_bullish, _ = calculate_supertrend(df)
        vol_avg20 = df["Volume"].tail(20).mean(); last_volume = float(df["Volume"].iloc[-1])
        rvol_raw = round(last_volume / vol_avg20, 2) if vol_avg20 > 0 else 0.0
        confidence = round(min(95.0, max(35.0, 50 + min(rvol_raw, 3) * 10 + rr_ratio * 3 + (10 if supertrend_bullish == is_bull else 0))), 1)
        gap_pct = 0.0
        if len(df) >= 2 and pd.notna(df["Close"].iloc[-2]) and df["Close"].iloc[-2] != 0:
            gap_pct = ((df["Open"].iloc[-1] - df["Close"].iloc[-2]) / df["Close"].iloc[-2]) * 100
        stock_ticker = symbol.replace("NSE:", "").replace("-EQ", "")
        signal_date_str, signal_time_str = (_format_signal_timestamp(event_ts, is_daily=True) if event_ts is not None else _candle_signal_timestamp(df, is_daily=True))
        return {"Signal Date": signal_date_str, "Signal Time": signal_time_str, "Symbol": stock_ticker, "LTP": round(last_close, 2), "Signal": "🟢 ▲ CISD BUY" if is_bull else "🔴 ▼ CISD SELL", "Entry": entry, "SL": sl, "Target": target, "Confidence": confidence, "Trend": supertrend_label, "Volume": int(last_volume), "RVOL": _format_rvol_display(rvol_raw), "News": calculate_news(stock_ticker, gap_pct, rvol_raw, "📈 Bullish" if is_bull else "📉 Bearish")}, None
    except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError, AttributeError) as e:
        return None, f"{symbol}: analysis error ({type(e).__name__})"


def run_fo_cisd_scan(fyers, symbols):
    """Threaded batch scan for the 'F&O CISD Scanner' tab. Logic unchanged."""
    symbols = _validate_symbols(symbols); results, errors = [], []
    stats = ScanStats(total=len(symbols)); progress = st.progress(0.0, text=f"Scanning F&O CISD 0 / {len(symbols)}"); done = 0
    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i:i + BATCH_SIZE]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(_fetch_fo_cisd_signal, fyers, s): s for s in batch}
            for future in as_completed(futures):
                try:
                    res, err = future.result()
                except Exception as e:
                    res, err = None, f"{futures[future]}: worker error ({type(e).__name__})"
                if res:
                    results.append(res)
                if err:
                    errors.append(err)
                stats.record(has_result=bool(res), has_error=bool(err)); done += 1
                progress.progress(done / len(symbols), text=f"Scanning F&O CISD {done} / {len(symbols)}")
        if i + BATCH_SIZE < len(symbols):
            time.sleep(BATCH_PAUSE_SECONDS)
    progress.empty(); gc.collect()
    return results, errors, stats


def _fetch_golden_death_cross_signal(fyers, symbol):
    """Per-symbol worker for the 'Golden/Death Cross' swing tab. Logic unchanged."""
    if not isinstance(symbol, str) or not _VALID_EQ_SYMBOL_RE.match(symbol):
        return None, f"{symbol}: invalid symbol format — skipped"
    resp, err = _safe_history(fyers, {"symbol": symbol, "resolution": "D", "date_format": "1", "range_from": DATE_FROM, "range_to": DATE_TO, "cont_flag": "1"})
    if err:
        return None, f"{symbol}: {err}"
    candles = resp.get("candles") if resp else None
    if not candles or len(candles) < 60:
        return None, f"{symbol}: insufficient history"
    try:
        df = pd.DataFrame(candles, columns=["Time", "Open", "High", "Low", "Close", "Volume"])
        df["Time"] = pd.to_datetime(df["Time"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
        df[["Open", "High", "Low", "Close", "Volume"]] = df[["Open", "High", "Low", "Close", "Volume"]].apply(pd.to_numeric, errors="coerce")
        df = df.dropna(subset=["Open", "High", "Low", "Close"])
        if len(df) < 60:
            return None, f"{symbol}: insufficient valid data"
        close = df["Close"]
        ema50 = close.ewm(span=50, adjust=False).mean()
        ema200 = close.ewm(span=200, adjust=False).mean() if len(close) >= 200 else close.ewm(span=len(close), adjust=False).mean()
        lookback = min(5, len(close) - 1); diff_tail = (ema50 - ema200).tail(lookback + 1)
        prev_sign = np.sign(diff_tail.iloc[0]); curr_sign = np.sign(diff_tail.iloc[-1])
        if prev_sign <= 0 and curr_sign > 0:
            cross_type = "Golden Cross"
        elif prev_sign >= 0 and curr_sign < 0:
            cross_type = "Death Cross"
        else:
            return None, None
        last_close = float(close.iloc[-1]); atr = float(calculate_atr(df).iloc[-1])
        if pd.isna(atr) or atr <= 0:
            atr = last_close * 0.01
        is_bull = cross_type == "Golden Cross"; entry = round(last_close, 2)
        if is_bull:
            sl = round(entry - 2.0 * atr, 2); t1 = round(entry + 2.0 * atr, 2); t2 = round(entry + 3.5 * atr, 2); t3 = round(entry + 5.0 * atr, 2)
        else:
            sl = round(entry + 2.0 * atr, 2); t1 = round(entry - 2.0 * atr, 2); t2 = round(entry - 3.5 * atr, 2); t3 = round(entry - 5.0 * atr, 2)
        atr_pct = (atr / last_close * 100) if last_close else 0
        if atr_pct >= 3:
            holding_days, est_days = "3–7 Days", 5
        elif atr_pct >= 1.5:
            holding_days, est_days = "7–14 Days", 10
        else:
            holding_days, est_days = "14–25 Days", 18
        exit_date = (_now_ist() + timedelta(days=est_days)).strftime("%d-%b-%Y")
        ema200_last = float(ema200.iloc[-1])
        ema_gap_pct = abs((float(ema50.iloc[-1]) - ema200_last) / ema200_last * 100) if ema200_last else 0
        trend_strength = "🟢 Strong" if ema_gap_pct >= 3 else ("🟡 Moderate" if ema_gap_pct >= 1 else "🔴 Weak")
        rsi_val = round(float(calculate_rsi(close).iloc[-1]), 1)
        vol_avg20 = df["Volume"].tail(20).mean()
        rvol_raw = round(float(df["Volume"].iloc[-1] / vol_avg20), 2) if vol_avg20 > 0 else 0.0
        ai_score = round(min(max(50 + (15 if is_bull else -15) + (rvol_raw * 8) + (rsi_val - 50) * 0.2, 0), 100), 1)
        confidence = round(min(95.0, max(35.0, 55 + ema_gap_pct * 4 + min(rvol_raw, 3) * 5)), 1)
        stock_ticker = symbol.replace("NSE:", "").replace("-EQ", "")
        signal_date_str, signal_time_str = _candle_signal_timestamp(df, is_daily=True)
        return {"Signal Date": signal_date_str, "Signal Time": signal_time_str, "Stock": stock_ticker, "Cross Type": cross_type, "Signal": "🟢 Swing BUY" if is_bull else "🔴 Swing SELL", "Entry": entry, "Stoploss": sl, "Target 1": t1, "Target 2": t2, "Target 3": t3, "Holding Period (Days)": holding_days, "Estimated Exit Date": exit_date, "Trend Strength": trend_strength, "Confidence %": confidence, "AI Score": ai_score, "News": calculate_news(stock_ticker, 0.0, rvol_raw, "📈 Bullish" if is_bull else "📉 Bearish")}, None
    except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError, AttributeError) as e:
        return None, f"{symbol}: analysis error ({type(e).__name__})"


def run_golden_death_cross_scan(fyers, symbols):
    """Threaded batch scan for the 'Golden/Death Cross' tab. Logic unchanged."""
    symbols = _validate_symbols(symbols); results, errors = [], []
    stats = ScanStats(total=len(symbols)); progress = st.progress(0.0, text=f"Scanning Golden/Death Cross 0 / {len(symbols)}"); done = 0
    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i:i + BATCH_SIZE]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(_fetch_golden_death_cross_signal, fyers, s): s for s in batch}
            for future in as_completed(futures):
                try:
                    res, err = future.result()
                except Exception as e:
                    res, err = None, f"{futures[future]}: worker error ({type(e).__name__})"
                if res:
                    results.append(res)
                if err:
                    errors.append(err)
                stats.record(has_result=bool(res), has_error=bool(err)); done += 1
                progress.progress(done / len(symbols), text=f"Scanning Golden/Death Cross {done} / {len(symbols)}")
        if i + BATCH_SIZE < len(symbols):
            time.sleep(BATCH_PAUSE_SECONDS)
    progress.empty(); gc.collect()
    return results, errors, stats


def _fetch_premarket_signal(fyers, symbol):
    """Per-symbol worker for the 'Pre-Market Scanner' tab. Logic unchanged."""
    if not isinstance(symbol, str) or not _VALID_EQ_SYMBOL_RE.match(symbol):
        return None, f"{symbol}: invalid symbol format — skipped"
    resp, err = _safe_history(fyers, {"symbol": symbol, "resolution": "D", "date_format": "1", "range_from": DATE_FROM, "range_to": DATE_TO, "cont_flag": "1"})
    if err:
        return None, f"{symbol}: {err}"
    candles = resp.get("candles") if resp else None
    if not candles or len(candles) < 30:
        return None, f"{symbol}: insufficient history"
    try:
        df = pd.DataFrame(candles, columns=["Time", "Open", "High", "Low", "Close", "Volume"])
        df["Time"] = pd.to_datetime(df["Time"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
        df[["Open", "High", "Low", "Close", "Volume"]] = df[["Open", "High", "Low", "Close", "Volume"]].apply(pd.to_numeric, errors="coerce")
        df = df.dropna(subset=["Open", "High", "Low", "Close"])
        if len(df) < 30:
            return None, f"{symbol}: insufficient valid data"
        recent = df.tail(10)
        buy_volume = float(recent.loc[recent["Close"] > recent["Open"], "Volume"].sum())
        sell_volume = float(recent.loc[recent["Close"] <= recent["Open"], "Volume"].sum())
        buy_sell_ratio = round(buy_volume / sell_volume, 2) if sell_volume > 0 else round(buy_volume, 2) if buy_volume > 0 else 0.0
        gap_pct = 0.0
        if len(df) >= 2 and pd.notna(df["Close"].iloc[-2]) and df["Close"].iloc[-2] != 0:
            gap_pct = ((df["Close"].iloc[-1] - df["Close"].iloc[-2]) / df["Close"].iloc[-2]) * 100
        vol_avg20 = df["Volume"].tail(20).mean()
        rvol_raw = round(float(df["Volume"].iloc[-1] / vol_avg20), 2) if vol_avg20 > 0 else 0.0
        rsi_val = round(float(calculate_rsi(df["Close"]).iloc[-1]), 1)
        ai_score = round(min(max(50 + (buy_sell_ratio - 1) * 8 + (rvol_raw * 6) + max(gap_pct, 0) * 2 + (rsi_val - 50) * 0.2, 0), 100), 1)
        bullish_votes = sum([buy_sell_ratio > 1.2, gap_pct > 0.3, rvol_raw >= 1.5, rsi_val > 50])
        bearish_votes = sum([buy_sell_ratio < 0.8, gap_pct < -0.3, rvol_raw >= 1.5, rsi_val < 50])
        if bullish_votes >= 3:
            expected_trend = "🟢 Bullish Opening Likely"
        elif bearish_votes >= 3:
            expected_trend = "🔴 Bearish Opening Likely"
        else:
            expected_trend = "🟡 Flat/Uncertain"
        stock_ticker = symbol.replace("NSE:", "").replace("-EQ", "")
        signal_date_str, signal_time_str = _candle_signal_timestamp(df, is_daily=True)
        return {"Signal Date": signal_date_str, "Signal Time": signal_time_str, "Stock": stock_ticker, "Buy Volume": int(buy_volume), "Sell Volume": int(sell_volume), "Buy/Sell Ratio": buy_sell_ratio, "Gap %": f"{gap_pct:.2f}%", "RVOL": _format_rvol_display(rvol_raw), "AI Score": ai_score, "Expected Opening Trend": expected_trend, "News": calculate_news(stock_ticker, gap_pct, rvol_raw, "📈 Bullish" if bullish_votes >= 3 else ("📉 Bearish" if bearish_votes >= 3 else "NO"))}, None
    except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError, AttributeError) as e:
        return None, f"{symbol}: analysis error ({type(e).__name__})"


def run_premarket_scan(fyers, symbols):
    """Threaded batch scan for the 'Pre-Market Scanner' tab. Logic unchanged."""
    symbols = _validate_symbols(symbols); results, errors = [], []
    stats = ScanStats(total=len(symbols)); progress = st.progress(0.0, text=f"Scanning Pre-Market 0 / {len(symbols)}"); done = 0
    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i:i + BATCH_SIZE]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(_fetch_premarket_signal, fyers, s): s for s in batch}
            for future in as_completed(futures):
                try:
                    res, err = future.result()
                except Exception as e:
                    res, err = None, f"{futures[future]}: worker error ({type(e).__name__})"
                if res:
                    results.append(res)
                if err:
                    errors.append(err)
                stats.record(has_result=bool(res), has_error=bool(err)); done += 1
                progress.progress(done / len(symbols), text=f"Scanning Pre-Market {done} / {len(symbols)}")
        if i + BATCH_SIZE < len(symbols):
            time.sleep(BATCH_PAUSE_SECONDS)
    progress.empty(); gc.collect()
    return results, errors, stats


FO_15M_CISD_RESOLUTION = "15"; FO_15M_CISD_RESOLUTION_MINUTES = 15; FO_15M_CISD_LOOKBACK_DAYS = 5


def _fetch_fo_15min_cisd_signal(fyers, symbol):
    """Per-symbol worker for the 'F&O 15-Min CISD Scanner' tab. Logic unchanged."""
    if not isinstance(symbol, str) or not _VALID_EQ_SYMBOL_RE.match(symbol):
        return None, f"{symbol}: invalid symbol format — skipped"
    date_from = (datetime.today() - timedelta(days=FO_15M_CISD_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    date_to = datetime.today().strftime("%Y-%m-%d")
    resp, err = _safe_history(fyers, {"symbol": symbol, "resolution": FO_15M_CISD_RESOLUTION, "date_format": "1", "range_from": date_from, "range_to": date_to, "cont_flag": "1"})
    if err:
        return None, f"{symbol}: {err}"
    candles = resp.get("candles") if resp else None
    if not candles or len(candles) < 31:
        return None, None
    try:
        df = pd.DataFrame(candles, columns=["Time", "Open", "High", "Low", "Close", "Volume"])
        df["Time"] = pd.to_datetime(df["Time"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
        df[["Open", "High", "Low", "Close", "Volume"]] = df[["Open", "High", "Low", "Close", "Volume"]].apply(pd.to_numeric, errors="coerce")
        df = df.dropna(subset=["Open", "High", "Low", "Close"]).sort_values("Time").reset_index(drop=True)
        if len(df) > 0 and not _is_intraday_candle_closed(df["Time"].iloc[-1], FO_15M_CISD_RESOLUTION_MINUTES):
            df = df.iloc[:-1].reset_index(drop=True)
        if len(df) < 30:
            return None, None
        smc_structure, cisd_signal, event_ts = _calculate_smc_and_cisd(df)
        if cisd_signal == "None" or event_ts is None:
            return None, None
        if not _is_intraday_candle_closed(event_ts, FO_15M_CISD_RESOLUTION_MINUTES):
            return None, None
        last_close = float(df["Close"].iloc[-1]); atr = float(calculate_atr(df).iloc[-1])
        if pd.isna(atr) or atr <= 0:
            atr = last_close * 0.005
        is_up = "Bullish" in cisd_signal; entry = round(last_close, 2)
        if is_up:
            sl = round(entry - 1.0 * atr, 2); t1 = round(entry + 1.0 * atr, 2); t2 = round(entry + 1.8 * atr, 2); t3 = round(entry + 2.6 * atr, 2)
        else:
            sl = round(entry + 1.0 * atr, 2); t1 = round(entry - 1.0 * atr, 2); t2 = round(entry - 1.8 * atr, 2); t3 = round(entry - 2.6 * atr, 2)
        risk = abs(entry - sl); reward = abs(t1 - entry); rr_ratio = round(reward / risk, 2) if risk > 0 else 0.0
        rsi_val = round(float(calculate_rsi(df["Close"]).iloc[-1]), 1)
        vol_avg20 = df["Volume"].tail(20).mean()
        rvol_raw = round(float(df["Volume"].iloc[-1] / vol_avg20), 2) if vol_avg20 > 0 else 0.0
        ai_score = round(min(max(50 + (rvol_raw * 10) + (10 if is_up else -10) + (rsi_val - 50) * 0.3, 0), 100), 1)
        confidence = round(min(95.0, max(35.0, 55 + min(rvol_raw, 3) * 8 + rr_ratio * 3)), 1)
        stock_ticker = symbol.replace("NSE:", "").replace("-EQ", "")
        signal_date_str, signal_time_str = _format_signal_timestamp(event_ts, is_daily=False)
        return {"Signal Date": signal_date_str, "Signal Time": signal_time_str, "Stock": stock_ticker, "LTP": round(last_close, 2), "CISD Signal": "🟢 ▲ CISD BUY" if is_up else "🔴 ▼ CISD SELL", "Entry": entry, "Stop Loss": sl, "Target 1": t1, "Target 2": t2, "Target 3": t3, "Confidence %": confidence, "AI Score": ai_score, "Reason": f"15-Min CISD {'bullish' if is_up else 'bearish'} shift on completed candle (RSI {rsi_val}, RVOL {_format_rvol_display(rvol_raw)})"}, None
    except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError, AttributeError) as e:
        return None, f"{symbol}: analysis error ({type(e).__name__})"


def run_fo_15min_cisd_scan(fyers, symbols):
    """Threaded batch scan for the 'F&O 15-Min CISD Scanner' tab. Logic unchanged."""
    symbols = _validate_symbols(symbols); results, errors = [], []
    stats = ScanStats(total=len(symbols)); progress = st.progress(0.0, text=f"Scanning F&O 15-Min CISD 0 / {len(symbols)}"); done = 0
    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i:i + BATCH_SIZE]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(_fetch_fo_15min_cisd_signal, fyers, s): s for s in batch}
            for future in as_completed(futures):
                try:
                    res, err = future.result()
                except Exception as e:
                    res, err = None, f"{futures[future]}: worker error ({type(e).__name__})"
                if res:
                    results.append(res)
                if err:
                    errors.append(err)
                stats.record(has_result=bool(res), has_error=bool(err)); done += 1
                progress.progress(done / len(symbols), text=f"Scanning F&O 15-Min CISD {done} / {len(symbols)}")
        if i + BATCH_SIZE < len(symbols):
            time.sleep(BATCH_PAUSE_SECONDS)
    progress.empty(); gc.collect()
    return results, errors, stats


def _load_seen_signal_keys() -> set:
    """Load the persisted set of already-notified Live-OB signal keys."""
    try:
        with open(_SEEN_SIGNALS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return set(data)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return set()


def _save_seen_signal_keys(keys: set) -> None:
    """Persist the (trimmed) set of seen Live-OB signal keys."""
    try:
        trimmed = sorted(keys)[-_SEEN_SIGNALS_MAX_KEEP:]
        with open(_SEEN_SIGNALS_FILE, "w", encoding="utf-8") as f:
            json.dump(trimmed, f)
    except OSError as e:
        logger.warning("Could not persist seen-signal keys: %s", e)


def _live_ob_signal_strength(volume_confirmed, smc_aligned, rvol_ok, macd_aligned, supertrend_aligned, ob_strength) -> str:
    c = 1 + int(volume_confirmed) + int(smc_aligned) + int(rvol_ok) + int(macd_aligned) + int(supertrend_aligned)
    if ob_strength == "Strong":
        c += 1
    return "🟢 Strong" if c >= 6 else ("🟡 Medium" if c >= 4 else "🔴 Weak")


def _fetch_live_ob_signal(fyers, symbol, seen_keys):
    """Per-symbol worker for the 'Live OB Signal Scanner' tab. Logic unchanged."""
    if not isinstance(symbol, str) or not _VALID_EQ_SYMBOL_RE.match(symbol):
        return None, f"{symbol}: invalid symbol format — skipped"
    date_from = (datetime.today() - timedelta(days=LIVE_OB_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    date_to = datetime.today().strftime("%Y-%m-%d")
    resp, err = _safe_history(fyers, {"symbol": symbol, "resolution": LIVE_OB_RESOLUTION, "date_format": "1", "range_from": date_from, "range_to": date_to, "cont_flag": "1"})
    if err:
        return None, f"{symbol}: {err}"
    candles = resp.get("candles") if resp else None
    if not candles or len(candles) < 31:
        return None, None
    try:
        df = pd.DataFrame(candles, columns=["Time", "Open", "High", "Low", "Close", "Volume"])
        df["Time"] = pd.to_datetime(df["Time"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
        df[["Open", "High", "Low", "Close", "Volume"]] = df[["Open", "High", "Low", "Close", "Volume"]].apply(pd.to_numeric, errors="coerce")
        df = df.dropna(subset=["Open", "High", "Low", "Close"]).sort_values("Time").reset_index(drop=True)
        if len(df) > 0 and not _is_intraday_candle_closed(df["Time"].iloc[-1], LIVE_OB_RESOLUTION_MINUTES):
            df = df.iloc[:-1].reset_index(drop=True)
        if len(df) < 30:
            return None, None
        smc_structure, cisd_signal, event_ts = _calculate_smc_and_cisd(df)
        bullish_ob, bearish_ob, ob_zone, ob_strength = _detect_order_blocks(df, smc_structure)
        if bullish_ob == "No" and bearish_ob == "No":
            return None, None
        direction = "BUY" if bullish_ob != "No" else "SELL"; is_buy = direction == "BUY"
        anchor_ts = event_ts if event_ts is not None else df["Time"].iloc[-1]
        signal_date_str, signal_time_str = _format_signal_timestamp(anchor_ts, is_daily=False)
        dedup_key = f"{symbol}|{LIVE_OB_RESOLUTION}|{signal_date_str}|{signal_time_str}|{direction}"
        if dedup_key in seen_keys:
            return None, None
        last_close = float(df["Close"].iloc[-1]); atr = float(calculate_atr(df).iloc[-1])
        if pd.isna(atr) or atr <= 0:
            atr = last_close * 0.005
        zone_low, zone_high = _parse_ob_zone(ob_zone)
        entry = round(last_close, 2)
        if is_buy:
            sl = round((zone_low - 0.25 * atr) if zone_low is not None else (entry - 1.0 * atr), 2)
            risk = max(entry - sl, 0.01); target1 = round(entry + 1.5 * risk, 2); target2 = round(entry + 3.0 * risk, 2)
        else:
            sl = round((zone_high + 0.25 * atr) if zone_high is not None else (entry + 1.0 * atr), 2)
            risk = max(sl - entry, 0.01); target1 = round(entry - 1.5 * risk, 2); target2 = round(entry - 3.0 * risk, 2)
        rr_ratio = round(abs(target1 - entry) / risk, 2) if risk > 0 else 0.0
        vol_avg20 = float(df["Volume"].tail(20).mean()); last_volume = float(df["Volume"].iloc[-1])
        volume_confirmed = bool(vol_avg20 > 0 and last_volume > vol_avg20)
        rvol_raw = round(last_volume / vol_avg20, 2) if vol_avg20 > 0 else 0.0
        rsi_val = round(float(calculate_rsi(df["Close"]).iloc[-1]), 1)
        macd_line, macd_sig, _ = calculate_macd(df["Close"]); macd_bullish = bool(macd_line.iloc[-1] > macd_sig.iloc[-1])
        supertrend_label, supertrend_bullish, _ = calculate_supertrend(df)
        smc_aligned = (is_buy and smc_structure in ("BOS 📈", "CHOCH 🐂")) or (not is_buy and smc_structure in ("BOS 📉", "CHOCH 🐻"))
        macd_aligned = (is_buy and macd_bullish) or (not is_buy and not macd_bullish)
        supertrend_aligned = (is_buy and supertrend_bullish is True) or (not is_buy and supertrend_bullish is False)
        signal_strength = _live_ob_signal_strength(volume_confirmed, smc_aligned, rvol_raw >= 1.5, macd_aligned, supertrend_aligned, ob_strength)
        stock_ticker = symbol.replace("NSE:", "").replace("-EQ", "")
        return {"dedup_key": dedup_key, "Signal Date": signal_date_str, "Signal Time": signal_time_str, "Stock": stock_ticker, "Symbol": symbol, "Direction": direction, "Signal": "🟢 BUY" if is_buy else "🔴 SELL", "LTP": entry, "Entry": entry, "Stop Loss": sl, "Target 1": target1, "Target 2": target2, "Risk:Reward": rr_ratio, "Order Block High": zone_high, "Order Block Low": zone_low, "Order Block Zone": ob_zone, "Order Block Strength": ob_strength, "Signal Strength": signal_strength, "Volume Confirmed": "✅ Yes" if volume_confirmed else "❌ No", "RVOL": _format_rvol_display(rvol_raw), "RSI": rsi_val, "MACD Signal": "🟢 Bullish" if macd_bullish else "🔴 Bearish", "Supertrend": supertrend_label, "SMC Structure": smc_structure, "CISD": cisd_signal}, None
    except Exception as e:
        logger.exception("Live OB error for %s", symbol)
        return None, f"{symbol}: error ({type(e).__name__})"


def _save_signal_txt(row, folder, base_name):
    """Persist a single Live-OB signal as a formatted text file."""
    path = os.path.join(folder, f"{base_name}.txt")
    try:
        lines = [f"NSE Live Order Block Signal — {row['Signal']}", "=" * 48, f"Stock            : {row['Stock']}", f"Signal Date/Time : {row['Signal Date']} {row['Signal Time']}", f"Direction        : {row['Direction']}", f"Entry            : {row['Entry']}", f"Stop Loss        : {row['Stop Loss']}", f"Target 1         : {row['Target 1']}", f"Target 2         : {row['Target 2']}", f"Risk:Reward      : {row['Risk:Reward']}", f"Order Block Zone : {row['Order Block Zone']}", f"Signal Strength  : {row['Signal Strength']}", f"Volume Confirmed : {row['Volume Confirmed']}", f"RVOL             : {row['RVOL']}", f"RSI              : {row['RSI']}", f"MACD Signal      : {row['MACD Signal']}", f"Supertrend       : {row['Supertrend']}", f"SMC Structure    : {row['SMC Structure']}", f"CISD             : {row['CISD']}"]
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return path
    except OSError as e:
        logger.warning("Could not write TXT for %s: %s", row.get("Stock"), e); return None


def _save_signal_json(row, folder, base_name):
    """Persist a single Live-OB signal to JSON and append it to the master JSON log."""
    path = os.path.join(folder, f"{base_name}.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(row, f, indent=2, default=str)
    except OSError as e:
        logger.warning("Could not write JSON for %s: %s", row.get("Stock"), e); return None
    try:
        history = []
        if os.path.exists(_LIVE_OB_MASTER_JSON):
            with open(_LIVE_OB_MASTER_JSON, "r", encoding="utf-8") as f:
                history = json.load(f)
        history.append(row)
        with open(_LIVE_OB_MASTER_JSON, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2, default=str)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Could not append to master JSON: %s", e)
    return path


def _save_signal_csv(row, folder, base_name):
    """Persist a single Live-OB signal to CSV and append it to the master CSV log."""
    path = os.path.join(folder, f"{base_name}.csv")
    fieldnames = [k for k in row.keys() if k != "dedup_key"]
    try:
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames); writer.writeheader(); writer.writerow({k: row[k] for k in fieldnames})
    except OSError as e:
        logger.warning("Could not write CSV for %s: %s", row.get("Stock"), e); return None
    try:
        master_exists = os.path.exists(_LIVE_OB_MASTER_CSV)
        with open(_LIVE_OB_MASTER_CSV, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if not master_exists:
                writer.writeheader()
            writer.writerow({k: row[k] for k in fieldnames})
    except OSError as e:
        logger.warning("Could not append to master CSV: %s", e)
    return path


def _save_signal_chart(df, row, folder, base_name):
    """Render and persist a candlestick chart PNG for a Live-OB signal (best-effort)."""
    if not MATPLOTLIB_AVAILABLE:
        return None
    path = os.path.join(folder, f"{base_name}.png")
    try:
        plot_df = df.tail(60).reset_index(drop=True)
        fig, ax = plt.subplots(figsize=(11, 6))
        for i, candle in plot_df.iterrows():
            color = "#26a69a" if candle["Close"] >= candle["Open"] else "#ef5350"
            ax.plot([i, i], [candle["Low"], candle["High"]], color=color, linewidth=1)
            ax.add_patch(plt.Rectangle((i - 0.3, min(candle["Open"], candle["Close"])), 0.6, max(abs(candle["Close"] - candle["Open"]), 1e-6), facecolor=color, edgecolor=color))
        zl, zh = row.get("Order Block Low"), row.get("Order Block High")
        if zl is not None and zh is not None:
            ax.axhspan(zl, zh, color="orange", alpha=0.2, label=f"OB Zone ({zl}-{zh})")
        ax.axhline(row["Entry"], color="blue", linestyle="--", linewidth=1.2, label=f"Entry {row['Entry']}")
        ax.axhline(row["Stop Loss"], color="red", linestyle="--", linewidth=1.2, label=f"SL {row['Stop Loss']}")
        ax.axhline(row["Target 1"], color="green", linestyle="--", linewidth=1.2, label=f"T1 {row['Target 1']}")
        ax.axhline(row["Target 2"], color="darkgreen", linestyle=":", linewidth=1.2, label=f"T2 {row['Target 2']}")
        ax.set_title(f"{row['Stock']} — {row['Signal']} @ {row['Signal Date']} {row['Signal Time']}")
        ax.legend(loc="best", fontsize=8); fig.tight_layout(); fig.savefig(path, dpi=120); plt.close(fig)
        return path
    except Exception as e:
        logger.warning("Could not save chart for %s: %s", row.get("Stock"), e)
        try:
            plt.close("all")
        except Exception:
            pass
        return None


def _persist_live_ob_signal(df, row) -> None:
    """Persist a Live-OB signal to disk (txt/json/csv/chart) and log it."""
    _ensure_app_folders()
    target_folder = SIGNALS_BUY_DIR if row["Direction"] == "BUY" else SIGNALS_SELL_DIR
    safe_time = row["Signal Time"].replace(":", "").replace(" ", "_")
    base_name = f"{row['Stock']}_{row['Signal Date']}_{safe_time}_{row['Direction']}"
    _save_signal_txt(row, target_folder, base_name); _save_signal_json(row, target_folder, base_name)
    _save_signal_csv(row, target_folder, base_name); _save_signal_chart(df, row, CHARTS_DIR, base_name)
    logger.info("New Live OB signal saved: %s %s @ %s %s", row["Stock"], row["Direction"], row["Signal Date"], row["Signal Time"])


def run_live_ob_signal_scan(fyers, symbols, seen_keys):
    """Threaded batch scan for the 'Live OB Signal Scanner' tab. Logic unchanged."""
    symbols = _validate_symbols(symbols); all_rows, new_rows, errors = [], [], []
    stats = ScanStats(total=len(symbols)); updated_keys = set(seen_keys)
    progress = st.progress(0.0, text=f"Scanning Live OB Signals 0 / {len(symbols)}"); done = 0
    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i:i + BATCH_SIZE]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(_fetch_live_ob_signal, fyers, s, seen_keys): s for s in batch}
            for future in as_completed(futures):
                try:
                    res, err = future.result()
                except Exception as e:
                    res, err = None, f"{futures[future]}: worker error ({type(e).__name__})"
                if res:
                    all_rows.append(res)
                    if res["dedup_key"] not in updated_keys:
                        updated_keys.add(res["dedup_key"]); new_rows.append(res)
                if err:
                    errors.append(err)
                stats.record(has_result=bool(res), has_error=bool(err)); done += 1
                progress.progress(done / max(len(symbols), 1), text=f"Scanning Live OB Signals {done} / {len(symbols)}")
        if i + BATCH_SIZE < len(symbols):
            time.sleep(BATCH_PAUSE_SECONDS)
    progress.empty(); _save_seen_signal_keys(updated_keys); gc.collect()
    return all_rows, new_rows, errors, stats, updated_keys


def _persist_new_live_ob_rows(fyers, new_rows) -> None:
    """Re-fetch candles and persist every newly-detected Live-OB signal."""
    for row in new_rows:
        try:
            date_from = (datetime.today() - timedelta(days=LIVE_OB_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
            date_to = datetime.today().strftime("%Y-%m-%d")
            resp, err = _safe_history(fyers, {"symbol": row["Symbol"], "resolution": LIVE_OB_RESOLUTION, "date_format": "1", "range_from": date_from, "range_to": date_to, "cont_flag": "1"})
            if err or not resp:
                continue
            candles = resp.get("candles")
            if not candles:
                continue
            df = pd.DataFrame(candles, columns=["Time", "Open", "High", "Low", "Close", "Volume"])
            df["Time"] = pd.to_datetime(df["Time"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
            df[["Open", "High", "Low", "Close", "Volume"]] = df[["Open", "High", "Low", "Close", "Volume"]].apply(pd.to_numeric, errors="coerce")
            df = df.dropna(subset=["Open", "High", "Low", "Close"]).sort_values("Time").reset_index(drop=True)
            _persist_live_ob_signal(df, row)
        except (KeyError, ValueError, TypeError, OSError) as e:
            logger.warning("Could not persist live OB signal for %s: %s", row.get("Stock"), e)


EMA_SWING_RESOLUTION = "240"; EMA_SWING_RESOLUTION_MINUTES = 240
EMA_SWING_LOOKBACK_DAYS = 400; EMA_SWING_FAST_SPAN = 50; EMA_SWING_SLOW_SPAN = 200; EMA_SWING_MIN_CANDLES = 60


def _fetch_ema_swing_signal(fyers, symbol):
    """Per-symbol worker for the 'EMA 50/200 Swing (4H)' tab. Logic unchanged."""
    if not isinstance(symbol, str) or not _VALID_EQ_SYMBOL_RE.match(symbol):
        return None, f"{symbol}: invalid symbol format — skipped"
    date_from = (datetime.today() - timedelta(days=EMA_SWING_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    date_to = datetime.today().strftime("%Y-%m-%d")
    resp, err = _safe_history(fyers, {"symbol": symbol, "resolution": EMA_SWING_RESOLUTION, "date_format": "1", "range_from": date_from, "range_to": date_to, "cont_flag": "1"})
    if err:
        return None, f"{symbol}: {err}"
    candles = resp.get("candles") if resp else None
    if not candles or len(candles) < EMA_SWING_MIN_CANDLES:
        return None, None
    try:
        df = pd.DataFrame(candles, columns=["Time", "Open", "High", "Low", "Close", "Volume"])
        df["Time"] = pd.to_datetime(df["Time"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
        df[["Open", "High", "Low", "Close", "Volume"]] = df[["Open", "High", "Low", "Close", "Volume"]].apply(pd.to_numeric, errors="coerce")
        df = df.dropna(subset=["Open", "High", "Low", "Close"]).sort_values("Time").reset_index(drop=True)
        if len(df) > 0 and not _is_intraday_candle_closed(df["Time"].iloc[-1], EMA_SWING_RESOLUTION_MINUTES):
            df = df.iloc[:-1].reset_index(drop=True)
        if len(df) < EMA_SWING_MIN_CANDLES:
            return None, None
        close = df["Close"]
        ema50 = close.ewm(span=EMA_SWING_FAST_SPAN, adjust=False).mean()
        ema200 = close.ewm(span=EMA_SWING_SLOW_SPAN, adjust=False).mean() if len(close) >= EMA_SWING_SLOW_SPAN else close.ewm(span=len(close), adjust=False).mean()
        lookback = min(5, len(close) - 1); diff_tail = (ema50 - ema200).tail(lookback + 1)
        prev_sign = np.sign(diff_tail.iloc[0]); curr_sign = np.sign(diff_tail.iloc[-1])
        if prev_sign <= 0 and curr_sign > 0:
            direction = "BUY"; golden_cross, death_cross = "Yes", "No"
        elif prev_sign >= 0 and curr_sign < 0:
            direction = "SELL"; golden_cross, death_cross = "No", "Yes"
        else:
            return None, None
        last_close = float(close.iloc[-1]); ema50_last = round(float(ema50.iloc[-1]), 2); ema200_last = round(float(ema200.iloc[-1]), 2)
        rsi_val = round(float(calculate_rsi(close).iloc[-1]), 1)
        macd_line, macd_signal_line, _ = calculate_macd(close); macd_bullish = bool(macd_line.iloc[-1] > macd_signal_line.iloc[-1])
        vol_avg20 = float(df["Volume"].tail(20).mean()); last_volume = float(df["Volume"].iloc[-1])
        volume_ok = bool(vol_avg20 > 0 and last_volume > vol_avg20); volume_ratio = round(last_volume / vol_avg20, 2) if vol_avg20 > 0 else 0.0
        vwap_val = calculate_vwap_approx(df); is_buy = direction == "BUY"
        if is_buy:
            confs = {"RSI>55": rsi_val > 55, "MACD Bullish": macd_bullish, "Volume>20avg": volume_ok, "Price>VWAP": vwap_val is not None and last_close > vwap_val}
        else:
            confs = {"RSI<45": rsi_val < 45, "MACD Bearish": not macd_bullish, "Volume>20avg": volume_ok, "Price<VWAP": vwap_val is not None and last_close < vwap_val}
        confirmed_count = sum(confs.values()); all_confirmed = confirmed_count == len(confs)
        if all_confirmed and is_buy:
            trade_decision = "🟢 BUY"
        elif all_confirmed and not is_buy:
            trade_decision = "🔴 SELL"
        else:
            trade_decision = "🟡 WATCH"
        ds = 1 if is_buy else -1
        ai_score = round(min(max(50 + ds * (confirmed_count * 10) + ds * min(volume_ratio, 3) * 3, 0), 100), 1)
        if confirmed_count == 4:
            swing_trend = "🟢🟢 Strong Bullish Reversal" if is_buy else "🔴🔴 Strong Bearish Reversal"
        elif confirmed_count >= 2:
            swing_trend = "🟢 Bullish Bias" if is_buy else "🔴 Bearish Bias"
        else:
            swing_trend = "🟡 Weak / Unconfirmed"
        atr = float(calculate_atr(df).iloc[-1])
        if pd.isna(atr) or atr <= 0:
            atr = last_close * 0.01
        entry = round(last_close, 2)
        if is_buy:
            stop_loss = round(entry - 2.0 * atr, 2); target1 = round(entry + 2.0 * atr, 2); target2 = round(entry + 3.5 * atr, 2)
        else:
            stop_loss = round(entry + 2.0 * atr, 2); target1 = round(entry - 2.0 * atr, 2); target2 = round(entry - 3.5 * atr, 2)
        stock_ticker = symbol.replace("NSE:", "").replace("-EQ", "")
        signal_date_str, signal_time_str = _candle_signal_timestamp(df, is_daily=False)
        return {"Signal Date": signal_date_str, "Signal Time": signal_time_str, "Stock": stock_ticker, "LTP": round(last_close, 2), "EMA 50": ema50_last, "EMA 200": ema200_last, "Golden Cross": golden_cross, "Death Cross": death_cross, "RSI": rsi_val, "MACD": "🟢 Bullish" if macd_bullish else "🔴 Bearish", "Volume Ratio": volume_ratio, "VWAP": vwap_val, "AI Score": ai_score, "Swing Trend": swing_trend, "Entry": entry, "Stop Loss": stop_loss, "Target 1": target1, "Target 2": target2, "Trade Decision": trade_decision}, None
    except Exception as e:
        return None, f"{symbol}: error ({type(e).__name__})"


def run_ema_swing_scan(fyers, symbols):
    """Threaded batch scan for the 'EMA 50/200 Swing (4H)' tab. Logic unchanged."""
    symbols = _validate_symbols(symbols); results, errors = [], []
    stats = ScanStats(total=len(symbols)); progress = st.progress(0.0, text=f"Scanning EMA 50/200 Swing (4H) 0 / {len(symbols)}"); done = 0
    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i:i + BATCH_SIZE]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(_fetch_ema_swing_signal, fyers, s): s for s in batch}
            for future in as_completed(futures):
                try:
                    res, err = future.result()
                except Exception as e:
                    res, err = None, f"{futures[future]}: worker error ({type(e).__name__})"
                if res:
                    results.append(res)
                if err:
                    errors.append(err)
                stats.record(has_result=bool(res), has_error=bool(err)); done += 1
                progress.progress(done / max(len(symbols), 1), text=f"Scanning EMA 50/200 Swing (4H) {done} / {len(symbols)}")
        if i + BATCH_SIZE < len(symbols):
            time.sleep(BATCH_PAUSE_SECONDS)
    progress.empty(); gc.collect()
    return results, errors, stats


# ══════════════════════════════════════════════════════════════════════════
# ── INSTITUTIONAL F&O OI ANALYSIS MODULE (OI/PCR/Max Pain math unchanged) ─
# ══════════════════════════════════════════════════════════════════════════

import functools
import hashlib

_OI_CACHE: dict = {}
_OI_CACHE_MAX = 500

_NSE_OC_URL = "https://www.nseindia.com/api/option-chain-equities?symbol={}"
_NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.nseindia.com/",
}


def _current_15m_epoch() -> int:
    """Epoch second of the START of the most recently COMPLETED 15-min candle."""
    now = int(_now_ist().timestamp())
    slot = (now // 900) * 900
    return slot - 900


def _nearest_atm(spot: float, strikes: list) -> float:
    if not strikes:
        return round(spot / 50) * 50
    return min(strikes, key=lambda s: abs(s - spot))


def _oi_cache_key(symbol: str) -> str:
    epoch = _current_15m_epoch()
    return f"{symbol}|{epoch}"


# ── OI Data Engine — sentinel statuses (Req #11: never silently zero-fill) ──
OI_SENTINEL_NO_CHAIN = "No Option Chain"
OI_SENTINEL_NO_EXPIRY = "Expiry Not Found"
OI_SENTINEL_EMPTY_API = "API Returned Empty Data"
OI_SENTINEL_NOT_AVAILABLE = "OI Not Available"
_OI_SENTINEL_VALUES = {OI_SENTINEL_NO_CHAIN, OI_SENTINEL_NO_EXPIRY, OI_SENTINEL_EMPTY_API, OI_SENTINEL_NOT_AVAILABLE}

# ── OI Data Engine — debug log ring buffer (Req #10/#14) ────────────────────
_OI_DEBUG_LOG: List[dict] = []
_OI_DEBUG_LOG_MAX = 300


def _oi_debug_record(**fields) -> None:
    """
    Append one structured diagnostic record (Symbol, Expiry, Spot Price,
    ATM Strike, API Response Status, Number of Strikes Received, API
    Source, Reason, and — when available — Sample CE/PE OI & change) to
    the in-memory debug ring buffer AND to scanner.log. Never raises, so a
    logging failure can never break the scan itself.
    """
    try:
        fields["_ts"] = _now_ist().strftime("%H:%M:%S")
        _OI_DEBUG_LOG.append(fields)
        if len(_OI_DEBUG_LOG) > _OI_DEBUG_LOG_MAX:
            del _OI_DEBUG_LOG[: len(_OI_DEBUG_LOG) - _OI_DEBUG_LOG_MAX]
        logger.info(
            "OI DEBUG | Symbol=%s Expiry=%s Spot=%s ATM=%s Status=%s Strikes=%s Source=%s Reason=%s "
            "SampleCE_OI=%s SamplePE_OI=%s SampleCE_Chg=%s SamplePE_Chg=%s",
            fields.get("Symbol"), fields.get("Expiry"), fields.get("Spot Price"),
            fields.get("ATM Strike"), fields.get("API Response Status"),
            fields.get("Number of Strikes Received"), fields.get("API Source"),
            fields.get("Reason"), fields.get("Sample CE OI"), fields.get("Sample PE OI"),
            fields.get("Sample CE Change"), fields.get("Sample PE Change"),
        )
    except Exception:
        pass


def _fyers_fetch_expiry_list(fyers, symbol: str) -> List[Tuple[str, int]]:
    """
    Req #2: Calls Fyers optionchain ONCE with strikecount=1/timestamp=""
    to retrieve the underlying's full expiry list. Returns
    [(date_str, epoch_seconds), ...] sorted ascending, or [] on any
    failure (never raises — callers treat [] as 'expiry not found').
    """
    try:
        resp = fyers.optionchain({"symbol": symbol, "strikecount": 1, "timestamp": ""})
    except Exception as e:
        logger.warning("Fyers expiry fetch failed for %s: %s", symbol, e)
        return []
    if not isinstance(resp, dict) or resp.get("s") != "ok":
        return []
    expiry_data = resp.get("data", {}).get("expiryData", []) or []
    out = []
    for item in expiry_data:
        try:
            epoch = int(item.get("expiry") or item.get("date_epoch") or 0)
            date_str = str(item.get("date") or item.get("expiry_date") or "")
            if epoch > 0:
                out.append((date_str, epoch))
        except (TypeError, ValueError):
            continue
    out.sort(key=lambda t: t[1])
    return out


def _pick_nearest_expiry(expiry_list: List[Tuple[str, int]]) -> Optional[Tuple[str, int]]:
    """Req #2: pick the nearest NON-expired expiry (epoch >= start of today IST)."""
    if not expiry_list:
        return None
    today_start = int(_now_ist().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    upcoming = [e for e in expiry_list if e[1] >= today_start]
    return upcoming[0] if upcoming else expiry_list[-1]


def _fyers_fetch_chain_for_expiry(fyers, symbol: str, expiry_epoch: int, strikecount: int = 10) -> Tuple[list, Optional[float], str]:
    """
    Req #3/#4: Fetch the FULL option chain for one expiry from Fyers.

    FIX: Fyers' real optionchain response is a FLAT list under
    data.optionsChain — each row has option_type ('CE'/'PE'/'' for the
    spot row), strike_price, oi, oich (OI change), volume, ltp, iv. There
    is NO nested 'CE'/'PE' sub-dict on each row. The previous parser
    assumed a nested schema (item['CE']['openInterest']) that never
    matches Fyers' actual payload, so ce_oi/pe_oi/oi-change were always
    read as 0 regardless of what the API returned. This pivots the flat
    rows into one dict per strike with real CE/PE OI + OI-change values.

    Returns (rows, spot_price_from_api, status) where status is
    'ok' | 'empty' | 'error'.
    """
    try:
        resp = fyers.optionchain({
            "symbol": symbol,
            "strikecount": strikecount,
            "timestamp": str(expiry_epoch),
        })
    except Exception as e:
        logger.warning("Fyers chain fetch error for %s: %s", symbol, e)
        return [], None, "error"

    if not isinstance(resp, dict) or resp.get("s") != "ok":
        return [], None, "error"

    chain = resp.get("data", {}).get("optionsChain", []) or []
    if not chain:
        return [], None, "empty"

    spot_price = None
    by_strike: Dict[float, dict] = {}
    for item in chain:
        try:
            opt_type = str(item.get("option_type", "")).upper()
            strike = float(item.get("strike_price", 0) or 0)
            if opt_type == "" and strike == 0:
                spot_price = float(item.get("ltp", 0) or 0) or spot_price
                continue
            if opt_type not in ("CE", "PE"):
                continue
            row = by_strike.setdefault(strike, {
                "strike": strike, "ce_oi": 0, "ce_oi_chg": 0, "pe_oi": 0, "pe_oi_chg": 0,
                "ce_vol": 0, "pe_vol": 0, "ce_ltp": 0.0, "pe_ltp": 0.0, "ce_iv": 0.0, "pe_iv": 0.0,
            })
            oi = int(item.get("oi", 0) or 0)
            oich = int(item.get("oich", 0) or 0)
            vol = int(item.get("volume", 0) or 0)
            ltp = float(item.get("ltp", 0) or 0)
            iv = float(item.get("iv", 0) or 0)
            if opt_type == "CE":
                row["ce_oi"] = oi; row["ce_oi_chg"] = oich; row["ce_vol"] = vol; row["ce_ltp"] = ltp; row["ce_iv"] = iv
            else:
                row["pe_oi"] = oi; row["pe_oi_chg"] = oich; row["pe_vol"] = vol; row["pe_ltp"] = ltp; row["pe_iv"] = iv
        except (TypeError, ValueError, KeyError):
            continue

    rows = list(by_strike.values())
    if not rows:
        return [], spot_price, "empty"
    return rows, spot_price, "ok"


def _nse_fetch_chain_for_expiry(symbol: str, expiry_date_str: Optional[str] = None) -> Tuple[list, Optional[float], Optional[str], str]:
    """
    Req #6: Backup path — NSE public option-chain-equities API.

    FIX: NSE's equity option-chain payload mixes rows from EVERY expiry
    into a single 'records.data' array (each row carries its own
    'expiryDate'). The previous implementation summed OI across ALL of
    them regardless of expiry, silently blending multiple expiries'
    open interest into one meaningless total. This now filters strictly
    to a single resolved expiry before returning any row.

    Returns (rows, spot_price, resolved_expiry_str, status) where status
    is 'ok' | 'empty' | 'error'.
    """
    try:
        ticker = symbol.replace("NSE:", "").replace("-EQ", "")
        session = requests.Session()
        session.get("https://www.nseindia.com", headers=_NSE_HEADERS, timeout=8)
        resp = session.get(_NSE_OC_URL.format(ticker), headers=_NSE_HEADERS, timeout=10)
        if resp.status_code != 200:
            return [], None, None, "error"
        payload = resp.json()
    except Exception as e:
        logger.warning("NSE chain fetch error for %s: %s", symbol, e)
        return [], None, None, "error"

    records = payload.get("records", {}) or {}
    data = records.get("data", []) or []
    spot_price = records.get("underlyingValue")
    try:
        spot_price = float(spot_price) if spot_price is not None else None
    except (TypeError, ValueError):
        spot_price = None
    expiry_dates = records.get("expiryDates", []) or []
    if not data:
        return [], spot_price, None, "empty"

    resolved_expiry = expiry_date_str if expiry_date_str in expiry_dates else (expiry_dates[0] if expiry_dates else None)
    if resolved_expiry is None:
        return [], spot_price, None, "empty"

    rows = []
    for item in data:
        if item.get("expiryDate") != resolved_expiry:
            continue
        try:
            strike = float(item.get("strikePrice", 0))
            ce = item.get("CE", {}) or {}
            pe = item.get("PE", {}) or {}
            rows.append({
                "strike": strike,
                "ce_oi": int(ce.get("openInterest", 0)),
                "ce_oi_chg": int(ce.get("changeinOpenInterest", 0)),
                "pe_oi": int(pe.get("openInterest", 0)),
                "pe_oi_chg": int(pe.get("changeinOpenInterest", 0)),
                "ce_vol": int(ce.get("totalTradedVolume", 0)),
                "pe_vol": int(pe.get("totalTradedVolume", 0)),
                "ce_ltp": float(ce.get("lastPrice", 0)),
                "pe_ltp": float(pe.get("lastPrice", 0)),
                "ce_iv": float(ce.get("impliedVolatility", 0)),
                "pe_iv": float(pe.get("impliedVolatility", 0)),
            })
        except (TypeError, ValueError, KeyError):
            continue

    if not rows:
        return [], spot_price, resolved_expiry, "empty"
    return rows, spot_price, resolved_expiry, "ok"


def _select_strikes(rows: list, spot: float, n_itm: int = 2, n_otm: int = 2) -> list:
    """Return ATM ± n_itm/n_otm strikes centred on spot. Logic unchanged."""
    if not rows:
        return []
    strikes = sorted(set(r["strike"] for r in rows))
    atm = _nearest_atm(spot, strikes)
    idx = strikes.index(atm) if atm in strikes else 0
    lo = max(0, idx - n_itm)
    hi = min(len(strikes), idx + n_otm + 1)
    selected = set(strikes[lo:hi])
    return [r for r in rows if r["strike"] in selected]


def _compute_oi_metrics(rows: list, prev_rows: Optional[list], spot: float, prev_spot: float, df_15m: pd.DataFrame) -> dict:
    """
    Given current + previous option chain rows and the 15-min OHLCV df,
    returns the full OI metric dict that becomes new columns.
    NOTE: OI/PCR/Max Pain/Probability math is UNCHANGED from the original.
    """
    if not rows:
        # Defensive fallback only — _fetch_fo_oi_signal now guards against
        # ever calling this with empty rows, but if it somehow happens,
        # never fabricate zeros: use the explicit sentinel instead (Req #11).
        return _sentinel_oi_row(OI_SENTINEL_NOT_AVAILABLE)

    total_ce_oi = sum(r["ce_oi"] for r in rows)
    total_pe_oi = sum(r["pe_oi"] for r in rows)
    total_ce_chg = sum(r["ce_oi_chg"] for r in rows)
    total_pe_chg = sum(r["pe_oi_chg"] for r in rows)
    total_ce_vol = sum(r["ce_vol"] for r in rows)
    total_pe_vol = sum(r["pe_vol"] for r in rows)

    net_oi_chg = total_pe_chg - total_ce_chg
    pcr = round(total_pe_oi / total_ce_oi, 3) if total_ce_oi else 0.0
    oi_ratio = round(total_ce_oi / total_pe_oi, 3) if total_pe_oi else 0.0

    try:
        def pain(k):
            return sum(
                r["ce_oi"] * max(0, k - r["strike"]) +
                r["pe_oi"] * max(0, r["strike"] - k)
                for r in rows
            )
        all_strikes = [r["strike"] for r in rows]
        max_pain_strike = min(all_strikes, key=pain) if all_strikes else spot
    except Exception:
        max_pain_strike = spot

    max_ce_row = max(rows, key=lambda r: r["ce_oi_chg"], default={})
    max_pe_row = max(rows, key=lambda r: r["pe_oi_chg"], default={})
    max_ce_writing_strike = max_ce_row.get("strike", 0)
    max_pe_writing_strike = max_pe_row.get("strike", 0)

    if prev_rows:
        prev_ce = {r["strike"]: r["ce_oi"] for r in prev_rows}
        prev_pe = {r["strike"]: r["pe_oi"] for r in prev_rows}
        cur_ce_sum = sum(r["ce_oi"] for r in rows)
        cur_pe_sum = sum(r["pe_oi"] for r in rows)
        prv_ce_sum = sum(prev_ce.get(r["strike"], r["ce_oi"]) for r in rows)
        prv_pe_sum = sum(prev_pe.get(r["strike"], r["pe_oi"]) for r in rows)
        ce_oi_delta_pct = round((cur_ce_sum - prv_ce_sum) / prv_ce_sum * 100, 2) if prv_ce_sum else 0.0
        pe_oi_delta_pct = round((cur_pe_sum - prv_pe_sum) / prv_pe_sum * 100, 2) if prv_pe_sum else 0.0
        oi_change_basis = "vs previous 15-min scan"
    else:
        total_ce_chg_now = sum(r["ce_oi_chg"] for r in rows)
        total_pe_chg_now = sum(r["pe_oi_chg"] for r in rows)
        total_ce_oi_now = sum(r["ce_oi"] for r in rows)
        total_pe_oi_now = sum(r["pe_oi"] for r in rows)
        prev_ce_session_total = total_ce_oi_now - total_ce_chg_now
        prev_pe_session_total = total_pe_oi_now - total_pe_chg_now
        ce_oi_delta_pct = round((total_ce_chg_now / prev_ce_session_total) * 100, 2) if prev_ce_session_total else 0.0
        pe_oi_delta_pct = round((total_pe_chg_now / prev_pe_session_total) * 100, 2) if prev_pe_session_total else 0.0
        oi_change_basis = "vs previous session close"

    price_chg_pct = round((spot - prev_spot) / prev_spot * 100, 3) if prev_spot else 0.0

    try:
        atr_val = float(calculate_atr(df_15m).iloc[-1])
        atr_pct = round(atr_val / spot * 100, 3) if spot else 0.0
    except Exception:
        atr_val = spot * 0.005
        atr_pct = 0.5

    try:
        vol_series = df_15m["Volume"]
        vol_avg = float(vol_series.tail(10).mean())
        vol_last = float(vol_series.iloc[-1])
        vol_accel = round((vol_last - vol_avg) / vol_avg * 100, 1) if vol_avg else 0.0
    except Exception:
        vol_accel = 0.0

    oi_accel = round(abs(ce_oi_delta_pct) + abs(pe_oi_delta_pct), 2)

    price_up = price_chg_pct > 0.05
    price_dn = price_chg_pct < -0.05
    ce_oi_up = ce_oi_delta_pct > 1
    ce_oi_dn = ce_oi_delta_pct < -1
    pe_oi_up = pe_oi_delta_pct > 1
    pe_oi_dn = pe_oi_delta_pct < -1

    if price_up and pe_oi_up and ce_oi_dn:
        oi_build_up = "Fresh Put Writing"; oi_bias = "🟢 Very Bullish"; inst_activity = "🏦 Institutional Long Build Up"; smart_money = "Bullish"
    elif price_up and ce_oi_dn and pe_oi_dn:
        oi_build_up = "Short Covering"; oi_bias = "🟢 Bullish"; inst_activity = "📈 Short Covering Detected"; smart_money = "Bullish"
    elif price_up and ce_oi_up and pe_oi_up:
        oi_build_up = "Long Build Up"; oi_bias = "🟢 Bullish"; inst_activity = "🏦 Long Build Up"; smart_money = "Bullish"
    elif price_dn and ce_oi_up and pe_oi_dn:
        oi_build_up = "Fresh Call Writing"; oi_bias = "🔴 Bearish"; inst_activity = "🐻 Institutional Short Build Up"; smart_money = "Bearish"
    elif price_dn and pe_oi_dn and ce_oi_dn:
        oi_build_up = "Long Unwinding"; oi_bias = "🔴 Bearish"; inst_activity = "📉 Long Unwinding"; smart_money = "Bearish"
    elif price_dn and ce_oi_up and pe_oi_up:
        oi_build_up = "Short Build Up"; oi_bias = "🔴 Strong Bearish"; inst_activity = "🔴 Strong Short Build Up"; smart_money = "Bearish"
    elif abs(price_chg_pct) <= 0.05 and pcr > 1.2:
        oi_build_up = "Range Bound - Bullish Bias"; oi_bias = "🟡 Neutral-Bullish"; inst_activity = "⚖️ Range Bound"; smart_money = "Neutral"
    elif abs(price_chg_pct) <= 0.05 and pcr < 0.8:
        oi_build_up = "Range Bound - Bearish Bias"; oi_bias = "🟡 Neutral-Bearish"; inst_activity = "⚖️ Range Bound"; smart_money = "Neutral"
    else:
        oi_build_up = "Neutral"; oi_bias = "🟡 Neutral"; inst_activity = "⚖️ No Clear Bias"; smart_money = "Neutral"

    fresh_call_writing = ce_oi_up and price_dn
    fresh_put_writing = pe_oi_up and price_up

    call_writing = "Yes" if fresh_call_writing else "No"
    put_writing = "Yes" if fresh_put_writing else "No"
    long_build_up = "Yes" if (price_up and ce_oi_up and pe_oi_up) else "No"
    short_build_up = "Yes" if (price_dn and ce_oi_up and pe_oi_up) else "No"
    long_unwinding = "Yes" if (price_dn and pe_oi_dn and ce_oi_dn) else "No"
    short_covering = "Yes" if (price_up and ce_oi_dn and pe_oi_dn) else "No"

    if long_build_up == "Yes":
        oi_pattern = "🟢🟢 Long Build Up — Both Legs Confirming"
    elif short_build_up == "Yes":
        oi_pattern = "🔴🔴 Short Build Up — Both Legs Confirming"
    elif long_unwinding == "Yes":
        oi_pattern = "🔴 Long Unwinding"
    elif short_covering == "Yes":
        oi_pattern = "🟢 Short Covering"
    elif put_writing == "Yes":
        oi_pattern = "🟢 Isolated Put Writing — CE OI Not Confirming"
    elif call_writing == "Yes":
        oi_pattern = "🔴 Isolated Call Writing — PE OI Not Confirming"
    else:
        oi_pattern = "🟡 No Clear OI Pattern"

    if pcr > 1.5:
        big_player = "🏦 Big Players Long"
    elif pcr < 0.6:
        big_player = "🐻 Big Players Short"
    elif 0.8 <= pcr <= 1.2:
        big_player = "⚖️ Balanced"
    else:
        big_player = "🟡 Neutral"

    atm_row = min(rows, key=lambda r: abs(r["strike"] - spot), default=None)
    atm_iv = ((atm_row["ce_iv"] + atm_row["pe_iv"]) / 2) if atm_row else 20.0
    if atm_iv <= 0:
        atm_iv = 20.0
    t_frac = 1 / 96
    exp_move_pct = round(atm_iv / 100 * (t_frac ** 0.5) * 100, 3)
    exp_move_pts = round(spot * exp_move_pct / 100, 2)
    exp_range_low = round(spot - exp_move_pts, 2)
    exp_range_high = round(spot + exp_move_pts, 2)

    oi_score = min(oi_accel * 3, 30)
    vol_score = min(max(vol_accel / 5, 0), 20)
    pcr_score = min(abs(pcr - 1.0) * 20, 20)
    atr_score = min(atr_pct * 4, 15)
    price_score = min(abs(price_chg_pct) * 10, 15)
    confidence = round(min(oi_score + vol_score + pcr_score + atr_score + price_score + 40, 97), 1)
    probability = round(min(confidence * 0.95, 95), 1)

    if "Bullish" in oi_bias or "Bullish" in oi_build_up:
        bullish_pct = round(min(50 + (pcr - 1) * 25 + abs(pe_oi_delta_pct) * 0.5, 95), 1)
        bearish_pct = round(100 - bullish_pct, 1)
    elif "Bearish" in oi_bias:
        bearish_pct = round(min(50 + (1 - pcr) * 25 + abs(ce_oi_delta_pct) * 0.5, 95), 1)
        bullish_pct = round(100 - bearish_pct, 1)
    else:
        bullish_pct = 50.0
        bearish_pct = 50.0

    if oi_accel > 10:
        oi_momentum = "🔥 High Acceleration"
    elif oi_accel > 5:
        oi_momentum = "⚡ Moderate"
    else:
        oi_momentum = "⚪ Low"

    if bullish_pct >= 70:
        next_candle = "📈 Likely Up"; trade_dir = "🟢 BUY"; exp_target = round(spot + exp_move_pts, 2); exp_sl = round(spot - exp_move_pts * 0.6, 2)
    elif bearish_pct >= 70:
        next_candle = "📉 Likely Down"; trade_dir = "🔴 SELL"; exp_target = round(spot - exp_move_pts, 2); exp_sl = round(spot + exp_move_pts * 0.6, 2)
    else:
        next_candle = "🟡 Range Bound"; trade_dir = "🟡 WAIT"; exp_target = spot; exp_sl = spot

    risk = abs(spot - exp_sl)
    reward = abs(exp_target - spot)
    rr = round(reward / risk, 2) if risk > 0 else 0.0

    ai_summary_parts = []
    ai_summary_parts.append(f"CE OI {'increased' if ce_oi_delta_pct > 0 else 'reduced'} by {abs(ce_oi_delta_pct):.1f}% ({oi_change_basis}).")
    ai_summary_parts.append(f"PE OI {'increased' if pe_oi_delta_pct > 0 else 'reduced'} by {abs(pe_oi_delta_pct):.1f}% ({oi_change_basis}).")
    if fresh_put_writing:
        ai_summary_parts.append("Fresh Put Writing observed.")
    if fresh_call_writing:
        ai_summary_parts.append("Fresh Call Writing observed.")
    ai_summary_parts.append(f"{inst_activity} detected.")
    direction_word = "upside" if bullish_pct > bearish_pct else "downside"
    ai_summary_parts.append(f"Expected {direction_word} move {exp_move_pct:.2f}%. Probability {probability}%.")
    ai_summary = " ".join(ai_summary_parts)

    if price_up and ce_oi_up and pe_oi_dn and oi_accel > 5:
        prediction_text = "⚠️ Trap Probability — False Breakout Risk"
    elif price_dn and pe_oi_up and ce_oi_dn and oi_accel > 5:
        prediction_text = "⚠️ Trap Probability — False Breakdown Risk"
    elif "Long Build Up" in oi_build_up and vol_accel > 10:
        prediction_text = "🚀 High Probability Breakout"
    elif "Short Build Up" in oi_build_up and vol_accel > 10:
        prediction_text = "🔻 High Probability Breakdown"
    elif fresh_call_writing and fresh_put_writing:
        prediction_text = "📦 Range Bound"
    elif fresh_put_writing:
        prediction_text = "📈 Strong Put Writing"
    elif fresh_call_writing:
        prediction_text = "📉 Strong Call Writing"
    elif "Short Covering" in oi_build_up:
        prediction_text = "📈 Short Covering"
    elif "Long Unwinding" in oi_build_up:
        prediction_text = "📉 Long Unwinding"
    elif "Institutional" in inst_activity and "Long" in inst_activity:
        prediction_text = "🏦 Institutional Buying"
    elif "Institutional" in inst_activity and "Short" in inst_activity:
        prediction_text = "🏦 Institutional Selling"
    else:
        prediction_text = "🟡 Neutral / No Strong Signal"

    return {
        "15m CE OI Δ": total_ce_chg,
        "15m PE OI Δ": total_pe_chg,
        "CE OI %": ce_oi_delta_pct,
        "PE OI %": pe_oi_delta_pct,
        "OI Change Basis": oi_change_basis,
        "Net OI": net_oi_chg,
        "PCR": pcr,
        "OI Ratio": oi_ratio,
        "Max Pain": max_pain_strike,
        "Max CE Writing": max_ce_writing_strike,
        "Max PE Writing": max_pe_writing_strike,
        "OI Bias": oi_bias,
        "OI Build Up": oi_build_up,
        "OI Momentum": oi_momentum,
        "OI Acceleration %": oi_accel,
        "Vol Acceleration %": vol_accel,
        "Price Change %": price_chg_pct,
        "ATR %": atr_pct,
        "Expected Move": f"{exp_move_pct:.3f}%",
        "Expected Points": exp_move_pts,
        "Expected Range": f"{exp_range_low}–{exp_range_high}",
        "Probability": f"{probability}%",
        "Confidence": f"{confidence}%",
        "Bullish %": bullish_pct,
        "Bearish %": bearish_pct,
        "Institutional Activity": inst_activity,
        "Smart Money Bias": smart_money,
        "Big Player Position": big_player,
        "Prediction Text": prediction_text,
        "Next Candle Prediction": next_candle,
        "Trade Direction": trade_dir,
        "Expected Target": exp_target,
        "Expected StopLoss": exp_sl,
        "Risk Reward": rr,
        "AI Summary": ai_summary,
        "Call Writing": call_writing,
        "Put Writing": put_writing,
        "Long Build Up": long_build_up,
        "Short Build Up": short_build_up,
        "Long Unwinding": long_unwinding,
        "Short Covering": short_covering,
        "OI Pattern": oi_pattern,
        "_ce_oi_total": total_ce_oi,
        "_pe_oi_total": total_pe_oi,
        "_atm_iv": atm_iv,
    }


def _empty_oi_row() -> dict:
    """
    DEPRECATED — kept only so nothing that references it at import time
    breaks. No longer called anywhere in the OI pipeline: every genuine
    failure now routes through `_sentinel_oi_row()` below, which reports
    the specific reason instead of a misleading zero (Req #11).
    """
    return _sentinel_oi_row(OI_SENTINEL_NOT_AVAILABLE)


def _sentinel_oi_row(reason: str) -> dict:
    """
    Req #11: returned whenever option-chain data is genuinely unavailable.
    Every OI-derived field is set to the exact sentinel string (`reason`)
    instead of 0/"—", so the report and Excel export show precisely why
    data is missing: 'No Option Chain', 'Expiry Not Found',
    'API Returned Empty Data', or 'OI Not Available'.
    """
    return {
        "15m CE OI Δ": reason, "15m PE OI Δ": reason, "CE OI %": reason, "PE OI %": reason,
        "OI Change Basis": reason,
        "Net OI": reason, "PCR": reason, "OI Ratio": reason, "Max Pain": reason,
        "Max CE Writing": reason, "Max PE Writing": reason,
        "OI Bias": reason, "OI Build Up": reason,
        "OI Momentum": reason, "OI Acceleration %": reason,
        "Vol Acceleration %": reason, "Price Change %": reason, "ATR %": reason,
        "Expected Move": reason, "Expected Points": reason, "Expected Range": reason,
        "Probability": reason, "Confidence": reason, "Bullish %": reason, "Bearish %": reason,
        "Institutional Activity": reason,
        "Smart Money Bias": reason, "Big Player Position": reason,
        "Prediction Text": reason,
        "Next Candle Prediction": reason, "Trade Direction": reason,
        "Expected Target": reason, "Expected StopLoss": reason, "Risk Reward": reason,
        "AI Summary": f"{reason} — signal generation skipped (requires valid OI data).",
        "Call Writing": reason, "Put Writing": reason, "Long Build Up": reason,
        "Short Build Up": reason, "Long Unwinding": reason, "Short Covering": reason,
        "OI Pattern": reason,
        "_ce_oi_total": reason, "_pe_oi_total": reason, "_atm_iv": reason,
    }


def _fetch_fo_oi_signal(fyers, symbol: str, fo_universe: Optional[set] = None) -> Tuple[Optional[dict], Optional[str]]:
    """
    Full OI pipeline for one F&O symbol — rewritten fetch/validate/compute
    layer (Req #1–#12). 15-min candle fetch and every technical indicator
    (RSI/ATR/EMA/VWAP/ADX/SMC/Support/Resistance) are UNCHANGED.

    Steps:
      1. Verify derivatives are actually available for this symbol.
      2. Detect the nearest valid (non-expired) expiry automatically.
      3. Fetch the complete option chain for that expiry from Fyers.
      4. Retry automatically if Fyers returns empty data.
      5. Fall back to NSE (expiry-aware) if Fyers still fails.
      6. Validate every row before any calculation.
      7. Only compute OI/AI metrics once valid data is confirmed.
      8. Never zero-fill — genuine failures return one of the 4 sentinel
         reasons and are still returned as a normal (non-error) result so
         they show up in the report instead of disappearing.
    """
    if not isinstance(symbol, str) or not _VALID_EQ_SYMBOL_RE.match(symbol):
        return None, f"{symbol}: invalid format"

    cache_key = _oi_cache_key(symbol)
    if cache_key in _OI_CACHE:
        return _OI_CACHE[cache_key], None

    stock_ticker = symbol.replace("NSE:", "").replace("-EQ", "")

    if fo_universe is not None and symbol not in fo_universe:
        _oi_debug_record(
            Symbol=stock_ticker, Expiry="—", **{"Spot Price": "—"}, **{"ATM Strike": "—"},
            **{"API Response Status": "skipped"}, **{"Number of Strikes Received": 0},
            **{"API Source": "—"}, Reason="Symbol has no listed derivatives (not in F&O universe)",
        )
        return None, f"{symbol}: {OI_SENTINEL_NO_CHAIN} — no listed derivatives"

    date_from = (datetime.today() - timedelta(days=5)).strftime("%Y-%m-%d")
    date_to = datetime.today().strftime("%Y-%m-%d")
    resp, err = _safe_history(fyers, {
        "symbol": symbol, "resolution": "15", "date_format": "1",
        "range_from": date_from, "range_to": date_to, "cont_flag": "1",
    })
    if err:
        return None, f"{symbol}: {err}"

    candles = resp.get("candles") if resp else None
    if not candles or len(candles) < 5:
        return None, f"{symbol}: insufficient 15m history"

    try:
        df = pd.DataFrame(candles, columns=["Time", "Open", "High", "Low", "Close", "Volume"])
        df["Time"] = pd.to_datetime(df["Time"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
        df[["Open", "High", "Low", "Close", "Volume"]] = df[["Open", "High", "Low", "Close", "Volume"]].apply(
            pd.to_numeric, errors="coerce"
        )
        df = df.dropna(subset=["Close"]).sort_values("Time").reset_index(drop=True)

        if len(df) > 0 and not _is_intraday_candle_closed(df["Time"].iloc[-1], 15):
            df = df.iloc[:-1].reset_index(drop=True)
        if len(df) < 3:
            return None, f"{symbol}: insufficient completed candles"

        spot = float(df["Close"].iloc[-1])
        prev_spot = float(df["Close"].iloc[-2])
        rsi_val = round(float(calculate_rsi(df["Close"]).iloc[-1]), 1)
        atr_val = round(float(calculate_atr(df).iloc[-1]), 2)
        ema20 = round(float(df["Close"].ewm(span=20).mean().iloc[-1]), 2)
        ema50 = round(float(df["Close"].ewm(span=50).mean().iloc[-1]), 2) if len(df) >= 50 else ema20
        vwap_val = calculate_vwap_approx(df)
        adx_val, _, _ = calculate_adx(df) if len(df) >= 15 else (0.0, 0.0, 0.0)
        smc_structure, cisd_signal, _ = _calculate_smc_and_cisd(df)
        signal_date, signal_time = _candle_signal_timestamp(df, is_daily=False)
        resistance = df["High"].rolling(20).max().shift(1).iloc[-1]
        support = df["Low"].rolling(20).min().shift(1).iloc[-1]
    except Exception as e:
        return None, f"{symbol}: candle processing error ({type(e).__name__})"

    base_fields = {
        "Signal Date": signal_date, "Signal Time": signal_time, "Stock": stock_ticker, "Spot": spot,
        "RSI (15m)": rsi_val, "ATR (15m)": atr_val, "EMA20 (15m)": ema20, "EMA50 (15m)": ema50,
        "VWAP (15m)": vwap_val, "ADX (15m)": adx_val, "SMC Structure": smc_structure, "CISD": cisd_signal,
        "Support": round(float(support), 2) if pd.notna(support) else None,
        "Resistance": round(float(resistance), 2) if pd.notna(resistance) else None,
    }

    expiry_list: List[Tuple[str, int]] = []
    for attempt in range(2):
        expiry_list = _fyers_fetch_expiry_list(fyers, symbol)
        if expiry_list:
            break
        time.sleep(0.5)

    nearest_expiry = _pick_nearest_expiry(expiry_list)
    expiry_date_str, expiry_epoch = (nearest_expiry if nearest_expiry else (None, None))

    rows: list = []
    api_source = "—"
    api_status = "error"

    if expiry_epoch is not None:
        for attempt in range(2):
            rows, _fyers_spot, api_status = _fyers_fetch_chain_for_expiry(fyers, symbol, expiry_epoch, strikecount=10)
            api_source = "Fyers"
            if rows:
                break
            time.sleep(0.5)

    if not rows:
        rows, _nse_spot, resolved_nse_expiry, nse_status = _nse_fetch_chain_for_expiry(symbol, expiry_date_str)
        api_source = "NSE"
        api_status = nse_status
        if resolved_nse_expiry:
            expiry_date_str = resolved_nse_expiry

    strikes_received = len(rows)

    reason = None
    if expiry_epoch is None and not expiry_date_str:
        reason = OI_SENTINEL_NO_EXPIRY
    elif not rows and api_status == "empty":
        reason = OI_SENTINEL_EMPTY_API
    elif not rows:
        reason = OI_SENTINEL_NO_CHAIN

    if rows:
        valid_rows = []
        for r in rows:
            try:
                if r.get("strike", 0) <= 0:
                    continue
                int(r.get("ce_oi", 0)); int(r.get("pe_oi", 0))
                valid_rows.append(r)
            except (TypeError, ValueError):
                continue
        rows = valid_rows
        if not rows:
            reason = OI_SENTINEL_NOT_AVAILABLE

    atm_for_log = _nearest_atm(spot, [r["strike"] for r in rows]) if rows else "—"
    _oi_debug_record(
        Symbol=stock_ticker, Expiry=expiry_date_str or "—", **{"Spot Price": spot},
        **{"ATM Strike": atm_for_log}, **{"API Response Status": api_status},
        **{"Number of Strikes Received": strikes_received}, **{"API Source": api_source},
        Reason=reason or "OK",
    )

    if reason:
        result = {
            **base_fields, "ATM Strike": "—", "OI Source": api_source,
            "Expiry": expiry_date_str or "—",
            **_sentinel_oi_row(reason), "_oc_rows": [],
        }
        if len(_OI_CACHE) >= _OI_CACHE_MAX:
            for k in sorted(_OI_CACHE.keys())[:100]:
                _OI_CACHE.pop(k, None)
        _OI_CACHE[cache_key] = result
        return result, None

    selected = _select_strikes(rows, spot)
    if not selected:
        selected = rows

    prev_cache_key = f"{symbol}|{_current_15m_epoch() - 900}"
    prev_rows = _OI_CACHE.get(prev_cache_key, {}).get("_oc_rows")

    metrics = _compute_oi_metrics(selected, prev_rows, spot, prev_spot, df)
    atm_strike = _nearest_atm(spot, [r["strike"] for r in selected])

    if selected:
        sample = selected[0]
        _oi_debug_record(
            Symbol=stock_ticker, Expiry=expiry_date_str or "—", **{"Spot Price": spot},
            **{"ATM Strike": atm_strike}, **{"API Response Status": "ok"},
            **{"Number of Strikes Received": len(selected)}, **{"API Source": api_source},
            **{"Sample CE OI": sample.get("ce_oi")}, **{"Sample PE OI": sample.get("pe_oi")},
            **{"Sample CE Change": sample.get("ce_oi_chg")}, **{"Sample PE Change": sample.get("pe_oi_chg")},
            Reason="OK",
        )

    result = {
        **base_fields,
        "ATM Strike": atm_strike,
        "OI Source": api_source,
        "Expiry": expiry_date_str or "—",
        **metrics,
        "_oc_rows": selected,
    }

    if len(_OI_CACHE) >= _OI_CACHE_MAX:
        for k in sorted(_OI_CACHE.keys())[:100]:
            _OI_CACHE.pop(k, None)
    _OI_CACHE[cache_key] = result
    return result, None


def run_fo_oi_scan(fyers, symbols: List[str]) -> Tuple[List[dict], List[str], "ScanStats"]:
    """
    Threaded F&O OI scan across `symbols` (should be F&O universe).
    Returns (results, errors, stats) — same shape as every other scanner.
    FIX #1: every successful `_fetch_fo_oi_signal` call is appended to
    `results` exactly once — this list is the single source of truth for
    the report pipeline built in `_build_fo_oi_report_df` (see below).
    Req #1: resolves the authoritative derivatives-available universe ONCE
    per scan and passes it to every worker for the availability check.
    """
    symbols = _validate_symbols(symbols)
    fo_master = set(load_nse_fo_stock_symbols())
    results: List[dict] = []
    errors: List[str] = []
    stats = ScanStats(total=len(symbols))
    progress = st.progress(0.0, text=f"F&O OI Scan 0 / {len(symbols)}")
    done = 0

    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i:i + BATCH_SIZE]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(_fetch_fo_oi_signal, fyers, s, fo_master): s for s in batch}
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
                progress.progress(
                    done / max(len(symbols), 1),
                    text=f"F&O OI Scan {done} / {len(symbols)}"
                )
        if i + BATCH_SIZE < len(symbols):
            time.sleep(BATCH_PAUSE_SECONDS)

    progress.empty()
    gc.collect()
    return results, errors, stats


def _oi_color_code(val) -> str:
    """Cell-level colour coding for the F&O OI report's string columns."""
    if not isinstance(val, str):
        return ""
    if val in _OI_SENTINEL_VALUES:
        return "color: #616161; font-weight: bold; background-color: #eeeeee; font-style: italic;"
    v = val.upper()
    if any(x in v for x in ["VERY BULLISH", "STRONG BULL", "INSTITUTIONAL BUYING", "LONG BUILD UP", "HIGH PROBABILITY BREAKOUT", "🟢🟢"]):
        return "color: #006100; font-weight: bold; background-color: #e8f5e9;"
    if any(x in v for x in ["BULLISH", "BUY", "PUT WRITING", "SHORT COVERING", "UPSIDE", "BREAKOUT", "🟢"]):
        return "color: #1b5e20; font-weight: bold;"
    if any(x in v for x in ["NEUTRAL", "RANGE", "BALANCED", "WAIT", "🟡", "⚖️"]):
        return "color: #b8860b; font-weight: bold;"
    if any(x in v for x in ["BEARISH", "SELL", "CALL WRITING", "UNWINDING", "BREAKDOWN", "INSTITUTIONAL SELLING", "🔴", "🟠"]):
        return "color: #b71c1c; font-weight: bold;"
    if any(x in v for x in ["STRONG BEAR", "STRONG SELL", "SHORT BUILD", "TRAP", "FALSE BREAK", "🔴🔴"]):
        return "color: #7f0000; font-weight: bold; background-color: #ffebee;"
    return ""


def _style_oi_df(df: pd.DataFrame):
    """Safe OI-aware styler — only applied to string columns."""
    try:
        str_cols = [c for c in df.columns if df[c].dtype == object]
        if not str_cols:
            return df.style
        styler = df.style
        if hasattr(styler, "map"):
            return styler.map(_oi_color_code, subset=str_cols)
        return styler.applymap(_oi_color_code, subset=str_cols)
    except Exception:
        try:
            return df.style
        except Exception:
            return df


# ══════════════════════════════════════════════════════════════════════════
# ── F&O OI REPORT PIPELINE (report generation / export only) ──────────────
# This section fixes the "successful stocks disappear" bug. It NEVER
# touches _fetch_fo_oi_signal, _compute_oi_metrics, run_fo_oi_scan, PCR,
# Max Pain, Probability, or any AI/signal calculation — it only reads the
# dicts those functions already produce and turns them into a report.
# ══════════════════════════════════════════════════════════════════════════

_FO_OI_REPORT_COLUMNS = [
    "Stock", "LTP", "Signal", "Direction", "OI Bias", "OI Status",
    "Spot Price", "ATM Strike", "Expiry", "PCR", "Max Pain",
    "CE OI", "PE OI", "CE OI Change", "PE OI Change",
    "OI Pattern", "Call Writing", "Put Writing", "Long Build Up", "Short Build Up",
    "Long Unwinding", "Short Covering",
    "Support", "Resistance",
    "Probability %", "Confidence %",
    "Entry", "Stop Loss", "Target 1", "Target 2", "Risk Reward",
    "AI Reason", "OI Source", "Timestamp",
]


def _pct_to_float(val, default: float = 0.0):
    """Safely turn '73.5%' / 73.5 / None into a float for sorting/filtering/export."""
    if val is None:
        return default
    if isinstance(val, str) and val in _OI_SENTINEL_VALUES:
        return np.nan
    try:
        return float(str(val).replace("%", "").strip())
    except (ValueError, TypeError):
        return default


def _oi_numeric_or_nan(value):
    """
    Req #11: convert an OI-engine value to float for the report/Excel/CSV,
    but if the value is one of the 4 sentinel strings (or otherwise not
    numeric), return NaN instead of silently fabricating a 0. NaN renders
    as a blank cell in Excel/CSV/JSON — clearly different from a real 0
    the API actually reported.
    """
    if isinstance(value, str):
        return np.nan
    try:
        return float(value)
    except (TypeError, ValueError):
        return np.nan


def _build_fo_oi_report_df(results: List[dict]) -> pd.DataFrame:
    """
    Build the report DIRECTLY from the raw results[] list (the exact same
    list every successful `_fetch_fo_oi_signal` call was appended to
    inside `run_fo_oi_scan`). One row is emitted per successful result.
    """
    rows = []
    for r in results:
        if not isinstance(r, dict):
            continue

        spot_raw = r.get("Spot", 0.0)
        try:
            spot = float(spot_raw) if spot_raw is not None else 0.0
        except (TypeError, ValueError):
            spot = 0.0

        oi_bias_val = r.get("OI Bias", "—")
        status = oi_bias_val if isinstance(oi_bias_val, str) and oi_bias_val in _OI_SENTINEL_VALUES else "OK"

        trade_dir = r.get("Trade Direction", "—")
        exp_target = _oi_numeric_or_nan(r.get("Expected Target", spot)) if status == "OK" else np.nan
        exp_pts = _oi_numeric_or_nan(r.get("Expected Points", 0.0)) if status == "OK" else np.nan

        if status == "OK" and isinstance(trade_dir, str) and "BUY" in trade_dir and not np.isnan(exp_pts):
            target2 = round(spot + 2 * exp_pts, 2)
        elif status == "OK" and isinstance(trade_dir, str) and "SELL" in trade_dir and not np.isnan(exp_pts):
            target2 = round(spot - 2 * exp_pts, 2)
        elif status == "OK":
            target2 = spot
        else:
            target2 = np.nan

        rows.append({
            "Stock": r.get("Stock", "—"),
            "LTP": round(spot, 2),
            "Signal": trade_dir if status == "OK" else status,
            "Direction": (r.get("Next Candle Prediction", "—") if status == "OK" else status),
            "OI Bias": oi_bias_val,
            "OI Status": status,
            "Spot Price": round(spot, 2),
            "ATM Strike": r.get("ATM Strike", "—"),
            "Expiry": r.get("Expiry", "—"),
            "PCR": _oi_numeric_or_nan(r.get("PCR", 0.0)),
            "Max Pain": _oi_numeric_or_nan(r.get("Max Pain", 0)),
            "CE OI": _oi_numeric_or_nan(r.get("_ce_oi_total", 0)),
            "PE OI": _oi_numeric_or_nan(r.get("_pe_oi_total", 0)),
            "CE OI Change": _oi_numeric_or_nan(r.get("15m CE OI Δ", 0)),
            "PE OI Change": _oi_numeric_or_nan(r.get("15m PE OI Δ", 0)),
            "OI Pattern": r.get("OI Pattern", status),
            "Call Writing": r.get("Call Writing", status),
            "Put Writing": r.get("Put Writing", status),
            "Long Build Up": r.get("Long Build Up", status),
            "Short Build Up": r.get("Short Build Up", status),
            "Long Unwinding": r.get("Long Unwinding", status),
            "Short Covering": r.get("Short Covering", status),
            "Support": r.get("Support"),
            "Resistance": r.get("Resistance"),
            "Probability %": _pct_to_float(r.get("Probability")),
            "Confidence %": _pct_to_float(r.get("Confidence")),
            "Entry": round(spot, 2),
            "Stop Loss": _oi_numeric_or_nan(r.get("Expected StopLoss", 0.0)) if status == "OK" else np.nan,
            "Target 1": exp_target,
            "Target 2": target2,
            "Risk Reward": _oi_numeric_or_nan(r.get("Risk Reward", 0.0)) if status == "OK" else np.nan,
            "AI Reason": (f"{r.get('Prediction Text', '—')} — {r.get('AI Summary', '')}".strip(" —") if status == "OK" else status),
            "OI Source": r.get("OI Source", "—"),
            "Timestamp": f"{r.get('Signal Date', '—')} {r.get('Signal Time', '—')}",
        })

    report_df = pd.DataFrame(rows, columns=_FO_OI_REPORT_COLUMNS)
    return report_df


def _build_fo_oi_excel(df: pd.DataFrame) -> bytes:
    """
    Dedicated professional Excel export for the F&O OI report.
    Title row + company header + generated time, bold coloured header row,
    freeze panes, autofilter, borders, center alignment, auto column width,
    and BUY=green / SELL=red / WATCH=yellow conditional row formatting.
    Exports EXACTLY the dataframe passed in — Displayed rows == Excel rows.
    """
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "F&O OI Report"

    thin = Side(style="thin", color="B0B0B0")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    center = Alignment(horizontal="center", vertical="center")
    n_cols = max(len(df.columns), 1)

    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=n_cols)
    company_cell = ws.cell(row=1, column=1, value="NSE AI PRO — Institutional F&O OI Analysis")
    company_cell.font = Font(bold=True, size=15, color="FFFFFF")
    company_cell.fill = PatternFill("solid", fgColor="0B2545")
    company_cell.alignment = center
    ws.row_dimensions[1].height = 28

    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=n_cols)
    title_cell = ws.cell(row=2, column=1, value=f"F&O OI Report — Generated {_now_ist().strftime('%d-%b-%Y %H:%M:%S')} IST")
    title_cell.font = Font(bold=True, size=12, color="FFFFFF")
    title_cell.fill = PatternFill("solid", fgColor="1F4E78")
    title_cell.alignment = center
    ws.row_dimensions[2].height = 22

    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill("solid", fgColor="2E5F8A")
    for c_idx, col_name in enumerate(df.columns, start=1):
        cell = ws.cell(row=3, column=c_idx, value=col_name)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = center
        cell.border = border

    buy_fill = PatternFill("solid", fgColor="C6EFCE")
    sell_fill = PatternFill("solid", fgColor="FFC7CE")
    wait_fill = PatternFill("solid", fgColor="FFEB9C")
    nodata_fill = PatternFill("solid", fgColor="E0E0E0")

    for r_idx, (_, row) in enumerate(df.iterrows(), start=4):
        signal_val = str(row.get("Signal", ""))
        oi_status_val = str(row.get("OI Status", "OK")) if "OI Status" in df.columns else "OK"
        if oi_status_val in _OI_SENTINEL_VALUES:
            row_fill = nodata_fill
        elif "BUY" in signal_val:
            row_fill = buy_fill
        elif "SELL" in signal_val:
            row_fill = sell_fill
        elif "WAIT" in signal_val or "WATCH" in signal_val:
            row_fill = wait_fill
        else:
            row_fill = None

        for c_idx, col_name in enumerate(df.columns, start=1):
            val = row[col_name]
            if isinstance(val, float) and np.isnan(val):
                val = None
            cell = ws.cell(row=r_idx, column=c_idx, value=val)
            cell.border = border
            cell.alignment = center
            if row_fill is not None:
                cell.fill = row_fill

    for c_idx, col_name in enumerate(df.columns, start=1):
        max_len = max(
            [len(str(col_name))] + [len(str(v)) for v in df[col_name].astype(str).tolist()]
        ) if len(df) else len(str(col_name))
        ws.column_dimensions[get_column_letter(c_idx)].width = min(max(max_len + 2, 10), 45)

    ws.freeze_panes = "A4"
    ws.auto_filter.ref = f"A3:{get_column_letter(n_cols)}{ws.max_row}"

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.getvalue()


def _show_fo_oi_debug_panel(total_symbols: int, stats: Optional["ScanStats"], rows_before: int, rows_after: int, rows_displayed: int, rows_exported: int, failed_count: int, report_df: Optional[pd.DataFrame] = None) -> None:
    """
    Debug panel showing every stage of the report pipeline PLUS the OI
    data-engine diagnostics (expiry detection, API source/status, strike
    counts, and sample OI values) for every symbol scanned, so a
    zero/blank cell can always be traced back to its cause.
    """
    with st.expander("🩺 Debug Panel — Report Pipeline & OI Data Engine", expanded=False):
        st.write({
            "Total Symbols": total_symbols,
            "Scanned": stats.scanned if stats else 0,
            "Successful": stats.successful if stats else 0,
            "Failed": stats.failed if stats else 0,
            "Rows Before Filter": rows_before,
            "Rows After Filter": rows_after,
            "Rows Displayed": rows_displayed,
            "Rows Exported": rows_exported,
        })
        if stats and stats.successful > 0 and rows_before < stats.successful:
            st.error("⚠️ report_df has fewer rows than successful scan results — pipeline leak detected.")

        if report_df is not None and not report_df.empty and "OI Status" in report_df.columns:
            status_counts = report_df["OI Status"].value_counts().to_dict()
            st.markdown("**OI Data Engine — Status Breakdown**")
            st.write(status_counts)

        if _OI_DEBUG_LOG:
            st.markdown("**OI Data Engine — Recent Fetch Diagnostics** (Symbol · Expiry · Spot · ATM · Status · Strikes · Source · Reason · Sample CE/PE OI & Change)")
            debug_df = pd.DataFrame(_OI_DEBUG_LOG[-100:])
            preferred = ["_ts", "Symbol", "Expiry", "Spot Price", "ATM Strike", "API Response Status",
                         "Number of Strikes Received", "API Source", "Reason",
                         "Sample CE OI", "Sample PE OI", "Sample CE Change", "Sample PE Change"]
            cols = [c for c in preferred if c in debug_df.columns] + [c for c in debug_df.columns if c not in preferred]
            st.dataframe(debug_df[cols].iloc[::-1], use_container_width=True, height=300, hide_index=True)
        else:
            st.caption("No OI fetch diagnostics recorded yet — run a scan to populate this log.")


def _show_fo_oi_failed_expander() -> None:
    """Failed symbols shown independently — never mixed into or subtracted from successful rows."""
    failed = st.session_state.get("fo_failed_symbols", [])
    if failed:
        with st.expander(f"⚠️ Failed/skipped symbols ({len(failed)})"):
            st.caption("Most stocks are skipped for missing/invalid data, not app errors.")
            st.text("\n".join(failed[:40]))


def _show_fo_oi_tab(fyers, fo_symbols: List[str]) -> None:
    """
    Renders the complete 🔬 F&O OI Analysis tab.
    Report pipeline hardened per the fixed data flow:
        Scanner → results[] → report_df → session_state.fo_report_df
        → Filters (never permanently hide data) → Displayed Report → Export
    No scanner/OI/AI/PCR/Max Pain/Probability logic is touched here.
    """
    st.markdown(
        "### 🔬 Institutional F&O OI Analysis — 15-Minute\n"
        "Tracks CE/PE Open Interest changes on the latest **completed 15-minute candle** "
        "for every F&O stock. Identifies Fresh Writing, Long/Short Build-Up, Institutional "
        "activity, and estimates the probability of the next directional move."
    )

    next_candle_at = _current_15m_epoch() + 900
    next_dt = datetime.fromtimestamp(next_candle_at, tz=IST).strftime("%H:%M:%S IST")
    st.caption(
        f"📡 OI Source: **Fyers** (NSE fallback) · "
        f"Data refreshes after every completed 15-min candle · "
        f"Next candle at **{next_dt}** · "
        f"Cache entries: {len(_OI_CACHE)}"
    )
    st.divider()

    oi_c1, oi_c2, oi_c3, oi_c4 = st.columns([1, 1, 1, 1])
    with oi_c1:
        oi_limit = st.number_input(
            "Limit F&O symbols (0 = all)", min_value=0,
            max_value=len(fo_symbols), value=min(50, len(fo_symbols)),
            step=25, key="oi_limit",
            help="Start with 50 to test. OI API calls are rate-limited."
        )
    with oi_c2:
        oi_bias_filter = st.selectbox(
            "Filter by Signal / Bias",
            ["All", "BUY only", "SELL only", "WATCH/WAIT only",
             "Bullish Bias", "Bearish Bias", "High Probability",
             "Institutional Activity"],
            key="oi_bias_filter"
        )
    with oi_c3:
        oi_min_prob = st.slider("Min Probability %", min_value=0, max_value=90, value=0, step=5, key="oi_min_prob")
    with oi_c4:
        if st.button("🗑️ Clear OI Cache", key="oi_clear_cache"):
            _OI_CACHE.clear()
            st.success("OI cache cleared.")

    oi_universe = fo_symbols if oi_limit == 0 else fo_symbols[:oi_limit]
    st.caption(f"Scanning **{len(oi_universe)}** F&O stocks.")

    if st.button(f"🔬 Run F&O OI Analysis ({len(oi_universe)} symbols)", key="oi_run"):
        with st.spinner("Fetching OI data from Fyers / NSE…"):
            oi_results, oi_errors, oi_stats = run_fo_oi_scan(fyers, oi_universe)

        report_df = _build_fo_oi_report_df(oi_results)

        st.session_state["fo_report_df"] = report_df
        st.session_state["fo_scan_results"] = oi_results
        st.session_state["fo_failed_symbols"] = oi_errors
        st.session_state["fo_oi_stats"] = oi_stats
        st.session_state["fo_oi_total_symbols"] = len(oi_universe)

    if "fo_oi_stats" in st.session_state:
        _display_scan_summary(st.session_state["fo_oi_stats"])

    report_df = st.session_state.get("fo_report_df")
    raw_results = st.session_state.get("fo_scan_results", [])

    if (report_df is None or report_df.empty) and raw_results:
        report_df = _build_fo_oi_report_df(raw_results)
        st.session_state["fo_report_df"] = report_df

    if report_df is None or report_df.empty:
        if "fo_scan_results" in st.session_state:
            st.info("Scan completed but produced no successful rows. Check failed symbols below.")
        else:
            st.info(
                "👆 Click **'Run F&O OI Analysis'** above to start.  \n"
                "The scanner fetches live option chain data for each F&O stock, "
                "computes CE/PE OI changes on the latest completed 15-min candle, "
                "and generates institutional-grade directional signals."
            )
        _show_fo_oi_failed_expander()
        return

    rows_before_filter = len(report_df)
    view = report_df.copy()

    try:
        if oi_bias_filter == "BUY only":
            view = view[view["Signal"].str.contains("BUY", na=False)]
        elif oi_bias_filter == "SELL only":
            view = view[view["Signal"].str.contains("SELL", na=False)]
        elif oi_bias_filter == "WATCH/WAIT only":
            view = view[view["Signal"].str.contains("WAIT|WATCH", na=False, regex=True)]
        elif oi_bias_filter == "Bullish Bias":
            view = view[view["OI Bias"].str.contains("Bullish", na=False)]
        elif oi_bias_filter == "Bearish Bias":
            view = view[view["OI Bias"].str.contains("Bearish", na=False)]
        elif oi_bias_filter == "High Probability":
            view = view[view["Probability %"] >= 70]
        elif oi_bias_filter == "Institutional Activity":
            view = view[view["AI Reason"].str.contains("Institutional|🏦", na=False, regex=True)]

        if oi_min_prob > 0:
            view = view[view["Probability %"] >= oi_min_prob]
    except Exception:
        view = report_df.copy()

    rows_after_filter = len(view)

    if rows_after_filter == 0 and rows_before_filter > 0:
        st.warning("⚠️ No stocks match current filters. Showing all successful results.")
        view = report_df.copy()
        rows_after_filter = len(view)

    view = view.sort_values("Probability %", ascending=False).reset_index(drop=True)

    st.markdown("#### 📊 OI Overview")
    k1, k2, k3, k4, k5, k6 = st.columns(6)
    k1.metric("Total Rows", len(view))
    try:
        k2.metric("🟢 BUY", int(view["Signal"].str.contains("BUY", na=False).sum()))
        k3.metric("🔴 SELL", int(view["Signal"].str.contains("SELL", na=False).sum()))
        k4.metric("🟡 WAIT/WATCH", int(view["Signal"].str.contains("WAIT|WATCH", na=False, regex=True).sum()))
    except Exception:
        k2.metric("🟢 BUY", "—"); k3.metric("🔴 SELL", "—"); k4.metric("🟡 WAIT/WATCH", "—")
    try:
        k5.metric("Avg Probability", f"{view['Probability %'].mean():.1f}%" if len(view) else "—")
    except Exception:
        k5.metric("Avg Probability", "—")
    try:
        k6.metric("Avg PCR", f"{view['PCR'].mean():.2f}" if len(view) else "—")
    except Exception:
        k6.metric("Avg PCR", "—")

    st.divider()

    st.markdown("#### 📋 F&O OI Report")
    try:
        st.dataframe(_style_oi_df(view), use_container_width=True, height=520)
    except Exception:
        st.dataframe(view, use_container_width=True, height=520)

    st.markdown("#### 💾 Export")
    ts = _now_ist().strftime("%Y%m%d_%H%M")
    dl1, dl2, dl3 = st.columns(3)
    with dl1:
        st.download_button(
            "📥 Download Excel (Professional)",
            data=_build_fo_oi_excel(view),
            file_name=f"FO_OI_Report_{ts}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="dl_oi_xlsx",
        )
    with dl2:
        st.download_button(
            "📥 Download CSV",
            data=to_csv_bytes(view),
            file_name=f"FO_OI_Report_{ts}.csv",
            mime="text/csv",
            key="dl_oi_csv",
        )
    with dl3:
        st.download_button(
            "📥 Download JSON",
            data=to_json_bytes(view),
            file_name=f"FO_OI_Report_{ts}.json",
            mime="application/json",
            key="dl_oi_json",
        )

    st.divider()

    show_cards = st.checkbox("📋 Show per-stock AI Summary cards", value=True, key="oi_show_cards")
    if show_cards:
        st.markdown(f"#### 🧠 AI OI Analysis Cards — {len(view)} stock(s)")
        if len(view) > 1:
            max_cards = st.slider("Max cards to show", 1, min(len(view), 30), min(10, len(view)), key="oi_max_cards")
        else:
            max_cards = len(view)

        for _, row in view.head(max_cards).iterrows():
            stock = str(row.get("Stock", "—"))
            oi_status = str(row.get("OI Status", "OK"))
            signal_val = str(row.get("Signal", "—"))
            is_bull = "BUY" in signal_val
            is_bear = "SELL" in signal_val
            card_color = "#006100" if is_bull else "#9C0006" if is_bear else "#b8860b"
            card_bg = "#f0fff0" if is_bull else "#fff0f0" if is_bear else "#fffde7"
            icon = "🟢" if is_bull else "🔴" if is_bear else "🟡"

            st.markdown(
                f'<div style="border:2px solid {card_color};border-radius:10px;'
                f'padding:14px;margin-bottom:6px;background:{card_bg}">'
                f'<h3 style="margin:0 0 4px 0;color:{card_color}">'
                f'{icon} {stock} &nbsp;|&nbsp; {row.get("OI Bias", "—")} &nbsp;|&nbsp; {signal_val}'
                f'</h3>'
                f'<span style="font-size:13px;color:#555">'
                f'📅 {row.get("Timestamp", "—")} &nbsp;·&nbsp; '
                f'Spot: ₹{row.get("Spot Price", 0):.2f} &nbsp;·&nbsp; ATM: {row.get("ATM Strike", "—")} &nbsp;·&nbsp; '
                f'Expiry: {row.get("Expiry", "—")} &nbsp;·&nbsp; Source: {row.get("OI Source", "—")}'
                f'</span></div>',
                unsafe_allow_html=True,
            )

            if oi_status != "OK":
                st.warning(f"⚠️ **{oi_status}** — option-chain data unavailable for {stock}; AI/OI signal generation skipped.")
                st.divider()
                continue

            def _sf(val, fmt="{:.2f}", default="—"):
                """NaN-safe formatter for card metrics."""
                try:
                    if val is None or (isinstance(val, float) and np.isnan(val)):
                        return default
                    return fmt.format(val)
                except (TypeError, ValueError):
                    return default

            rc1, rc2, rc3, rc4, rc5 = st.columns(5)
            rc1.metric("📍 Direction", row.get("Direction", "—"))
            rc2.metric("🎯 Probability", f"{_sf(row.get('Probability %'), '{:.1f}')}%")
            rc3.metric("💡 Confidence", f"{_sf(row.get('Confidence %'), '{:.1f}')}%")
            rc4.metric("⚖️ PCR", _sf(row.get('PCR'), '{:.3f}'))
            rc5.metric("💊 Max Pain", f"₹{_sf(row.get('Max Pain'), '{:.0f}')}")

            oc1, oc2, oc3, oc4, oc5 = st.columns(5)
            oc1.metric("📞 CE OI", _sf(row.get('CE OI'), '{:,.0f}'))
            oc2.metric("📤 PE OI", _sf(row.get('PE OI'), '{:,.0f}'))
            oc3.metric("📞 CE Δ", _sf(row.get('CE OI Change'), '{:,.0f}'))
            oc4.metric("📤 PE Δ", _sf(row.get('PE OI Change'), '{:,.0f}'))
            oc5.metric("⚖️ Risk:Reward", f"1:{_sf(row.get('Risk Reward'), '{:.2f}')}")

            st.markdown(f"**🧩 OI Pattern:** {row.get('OI Pattern', '—')}")

            wc1, wc2, wc3, wc4 = st.columns(4)
            wc1.metric("✍️ Call Writing", row.get("Call Writing", "—"))
            wc2.metric("✍️ Put Writing", row.get("Put Writing", "—"))
            wc3.metric("📈 Long Build Up", row.get("Long Build Up", "—"))
            wc4.metric("📉 Short Build Up", row.get("Short Build Up", "—"))

            em1, em2, em3, em4 = st.columns(4)
            em1.metric("🎯 Entry", f"₹{_sf(row.get('Entry'))}")
            em2.metric("🛑 Stop Loss", f"₹{_sf(row.get('Stop Loss'))}")
            em3.metric("🎯 Target 1", f"₹{_sf(row.get('Target 1'))}")
            em4.metric("🎯 Target 2", f"₹{_sf(row.get('Target 2'))}")

            st.markdown("**🤖 AI Reason:**")
            ai_reason = str(row.get("AI Reason", "—"))
            if is_bull:
                st.success(ai_reason)
            elif is_bear:
                st.error(ai_reason)
            else:
                st.warning(ai_reason)

            st.divider()

    _show_fo_oi_debug_panel(
        total_symbols=st.session_state.get("fo_oi_total_symbols", len(oi_universe)),
        stats=st.session_state.get("fo_oi_stats"),
        rows_before=rows_before_filter,
        rows_after=rows_after_filter,
        rows_displayed=len(view),
        rows_exported=len(view),
        failed_count=len(st.session_state.get("fo_failed_symbols", [])),
        report_df=view,
    )

    _show_fo_oi_failed_expander()


def show_scanner(fyers) -> None:
    """Top-level Streamlit entry point — renders every scanner tab."""
    st.title("🚀 NSE AI PRO V13 — Institutional Scanner")
    st.caption(f"🕒 Current Time (IST): {_now_ist().strftime('%d-%b-%Y %H:%M:%S')} IST")
    symbols = load_nse_equity_symbols()
    st.caption(f"Loaded {len(symbols)} NSE equity symbols from Fyers symbol master.")
    if not symbols:
        st.warning("No symbols loaded — check network access to public.fyers.in.")
        return

    col1, col2, col3 = st.columns([1, 1, 2])
    with col1:
        limit = st.number_input("Limit symbols (0 = all)", min_value=0, max_value=len(symbols), value=200, step=50)
    with col2:
        enable_xgboost = st.checkbox("Enable XGBoost ML training", value=False, disabled=not XGBOOST_AVAILABLE)
    with col3:
        st.caption(f"~{((limit or len(symbols)) / MAX_WORKERS) * 0.3 / 60:.1f}–{((limit or len(symbols)) / MAX_WORKERS) * 1.0 / 60:.1f} min estimated.")
    scan_universe = symbols if limit == 0 else symbols[:limit]

    if st.button(f"🚀 Run Scan ({len(scan_universe)} symbols)"):
        with st.spinner("Fetching NIFTY benchmark…"):
            nifty_close = fetch_nifty_benchmark(fyers)
        with st.spinner("Scanning…"):
            results, errors, stats = run_scan(fyers, scan_universe, nifty_close, enable_xgboost)
            full_df = pd.DataFrame(results)
            if not full_df.empty and "_Is_High_Quality" in full_df.columns:
                full_df = full_df[full_df["_Is_High_Quality"] == True]
            display_cols = [c for c in full_df.columns if not c.startswith("_")]
            scan_df = full_df[display_cols] if not full_df.empty else full_df
            intraday_df = pd.DataFrame([calculate_intraday_signal(r) for r in results])
            swing_df = pd.DataFrame([calculate_swing_signal(r) for r in results])
        st.session_state["scan_df"] = scan_df; st.session_state["intraday_df"] = intraday_df
        st.session_state["swing_df"] = swing_df; st.session_state["scan_errors"] = errors; st.session_state["scan_stats"] = stats
        del full_df
        gc.collect()

    if "scan_stats" in st.session_state:
        _display_scan_summary(st.session_state["scan_stats"])

    (tab_scanner, tab_intraday, tab_swing, tab_fo, tab_intraday_cisd, tab_fo_cisd,
     tab_golden_death, tab_premarket, tab_fo_15m_cisd, tab_live_ob, tab_ema_swing,
     tab_institutional, tab_fo_oi) = st.tabs([
        "📊 Full Scanner", "⚡ Intraday Scanner", "📈 Swing Trade Scanner", "🏛️ F&O Stocks Scanner",
        "🕐 Intraday CISD Signals", "🎯 F&O CISD Scanner", "✝️ Swing Trading (Golden/Death Cross)",
        "🌅 Pre-Market Scanner", "🎯 NSE F&O 15-Min CISD Scanner", "🔔 Live OB Signal Scanner",
        "🌟 EMA 50/200 Swing (4H)", "🏆 Institutional Scanner", "🔬 F&O OI Analysis",
    ])

    with tab_scanner:
        st.caption(f"High-Quality signals only — ≥{SIGNAL_QUALITY_MIN_CONFIRMATIONS}/10 conditions confirmed. Institutional Price Action columns (Swing Structure, BOS/CHOCH, Decision, Institutional Grade, etc.) are included automatically.")
        if "scan_df" in st.session_state:
            df = st.session_state["scan_df"]
            if df.empty:
                st.info("No stocks met the high-quality bar for this scan.")
            else:
                sorted_df = df.sort_values("AI Score", ascending=False)
                st.dataframe(_style_dataframe(sorted_df), use_container_width=True, height=500)
                st.bar_chart(df.set_index("Stock")["AI Score"])
                st.download_button("📥 Download Full Scan as Excel", data=to_excel_bytes(sorted_df, "Scan Results"), file_name=f"nse_scan_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="dl_scan")
        else:
            st.info("Run a scan above to see Full Scanner results here.")

    with tab_intraday:
        idf = st.session_state.get("intraday_df")
        if idf is not None and not idf.empty:
            idf_sorted = idf.sort_values("Confidence %", ascending=False)
            st.dataframe(_style_dataframe(idf_sorted), use_container_width=True, height=500)
            st.download_button("📥 Download Intraday Signals as Excel", data=to_excel_bytes(idf_sorted, "Intraday Signals"), file_name=f"nse_intraday_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="dl_intraday")
        else:
            st.info("Run a scan above to see Intraday Scanner results here.")

    with tab_swing:
        sdf = st.session_state.get("swing_df")
        if sdf is not None and not sdf.empty:
            sdf_sorted = sdf.sort_values("Confidence %", ascending=False)
            st.dataframe(_style_dataframe(sdf_sorted), use_container_width=True, height=500)
            st.download_button("📥 Download Swing Signals as Excel", data=to_excel_bytes(sdf_sorted, "Swing Signals"), file_name=f"nse_swing_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="dl_swing")
        else:
            st.info("Run a scan above to see Swing Trade Scanner results here.")

    with tab_fo:
        fo_symbols = load_nse_fo_stock_symbols()
        st.caption(f"Loaded {len(fo_symbols)} F&O-permitted NSE stocks.")
        if not fo_symbols:
            st.warning("No F&O symbols loaded.")
        else:
            fo_col1, fo_col2 = st.columns([1, 1])
            with fo_col1:
                fo_limit = st.number_input("Limit F&O symbols (0=all)", min_value=0, max_value=len(fo_symbols), value=len(fo_symbols), step=25, key="fo_limit")
            with fo_col2:
                fo_enable_xgboost = st.checkbox("Enable XGBoost (F&O)", value=False, key="fo_xgb", disabled=not XGBOOST_AVAILABLE)
            fo_universe = fo_symbols if fo_limit == 0 else fo_symbols[:fo_limit]
            if st.button(f"🏛️ Run F&O Stocks Scan ({len(fo_universe)} symbols)", key="fo_run"):
                with st.spinner("Scanning F&O stocks…"):
                    fo_nifty_close = fetch_nifty_benchmark(fyers)
                    fo_results, fo_errors, fo_stats = run_scan(fyers, fo_universe, fo_nifty_close, fo_enable_xgboost)
                    fo_full_df = pd.DataFrame(fo_results)
                    if not fo_full_df.empty and "_Is_High_Quality" in fo_full_df.columns:
                        fo_full_df = fo_full_df[fo_full_df["_Is_High_Quality"] == True]
                    fo_dc = [c for c in fo_full_df.columns if not c.startswith("_")]
                    fo_scan_df = fo_full_df[fo_dc] if not fo_full_df.empty else fo_full_df
                st.session_state["fo_scan_df"] = fo_scan_df; st.session_state["fo_scan_errors"] = fo_errors; st.session_state["fo_scan_stats"] = fo_stats
                gc.collect()
            if "fo_scan_stats" in st.session_state:
                _display_scan_summary(st.session_state["fo_scan_stats"])
            fo_df = st.session_state.get("fo_scan_df")
            if fo_df is not None and not fo_df.empty:
                fo_sorted = fo_df.sort_values("AI Score", ascending=False)
                st.dataframe(_style_dataframe(fo_sorted), use_container_width=True, height=500)
                st.bar_chart(fo_df.set_index("Stock")["AI Score"])
                st.download_button("📥 Download F&O Scan as Excel", data=to_excel_bytes(fo_sorted, "F&O Stocks"), file_name=f"nse_fo_scan_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="dl_fo")
            elif "fo_scan_df" in st.session_state:
                st.info("No F&O stocks met the high-quality bar.")
            else:
                st.info("Run an F&O scan above.")
            if st.session_state.get("fo_scan_errors"):
                with st.expander(f"⚠️ Skipped F&O symbols ({len(st.session_state['fo_scan_errors'])})"):
                    st.text("\n".join(st.session_state["fo_scan_errors"][:20]))

    with tab_intraday_cisd:
        ic1, ic2, _ = st.columns([1, 1, 1])
        with ic1:
            icisd_tf = st.selectbox("Timeframe", list(_INTRADAY_RESOLUTION_MAP.keys()), key="icisd_timeframe")
        with ic2:
            icisd_lim = st.number_input("Limit (0=all)", min_value=0, max_value=len(symbols), value=min(200, len(symbols)), step=50, key="icisd_limit")
        icisd_universe = symbols if icisd_lim == 0 else symbols[:icisd_lim]
        if st.button(f"🕐 Run Intraday CISD Scan ({len(icisd_universe)} symbols, {icisd_tf})", key="icisd_run"):
            with st.spinner("Scanning…"):
                icisd_results, icisd_errors, icisd_stats = run_intraday_cisd_scan(fyers, icisd_universe, _INTRADAY_RESOLUTION_MAP[icisd_tf], icisd_tf)
                st.session_state["intraday_cisd_df"] = pd.DataFrame(icisd_results); st.session_state["intraday_cisd_errors"] = icisd_errors; st.session_state["intraday_cisd_stats"] = icisd_stats
        if "intraday_cisd_stats" in st.session_state:
            _display_scan_summary(st.session_state["intraday_cisd_stats"])
        icisd_df = st.session_state.get("intraday_cisd_df")
        if icisd_df is not None and not icisd_df.empty:
            st.dataframe(_style_dataframe(icisd_df.sort_values("Confidence %", ascending=False)), use_container_width=True, height=500)
            st.download_button("📥 Download as Excel", data=to_excel_bytes(icisd_df, "Intraday CISD"), file_name=f"nse_intraday_cisd_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="dl_icisd")
        else:
            st.info("Run an Intraday CISD scan above.")
        if st.session_state.get("intraday_cisd_errors"):
            with st.expander(f"⚠️ Skipped ({len(st.session_state['intraday_cisd_errors'])})"):
                st.text("\n".join(st.session_state["intraday_cisd_errors"][:20]))

    with tab_fo_cisd:
        fo_cisd_symbols = load_nse_fo_stock_symbols()
        if not fo_cisd_symbols:
            st.warning("No F&O symbols loaded.")
        else:
            fo_cisd_lim = st.number_input("Limit (0=all)", min_value=0, max_value=len(fo_cisd_symbols), value=len(fo_cisd_symbols), step=25, key="fo_cisd_limit")
            fo_cisd_universe = fo_cisd_symbols if fo_cisd_lim == 0 else fo_cisd_symbols[:fo_cisd_lim]
            if st.button(f"🎯 Run F&O CISD Scan ({len(fo_cisd_universe)} symbols)", key="fo_cisd_run"):
                with st.spinner("Scanning…"):
                    fo_cisd_results, fo_cisd_errors, fo_cisd_stats = run_fo_cisd_scan(fyers, fo_cisd_universe)
                    st.session_state["fo_cisd_df"] = pd.DataFrame(fo_cisd_results); st.session_state["fo_cisd_errors"] = fo_cisd_errors; st.session_state["fo_cisd_stats"] = fo_cisd_stats
            if "fo_cisd_stats" in st.session_state:
                _display_scan_summary(st.session_state["fo_cisd_stats"])
            fo_cisd_df = st.session_state.get("fo_cisd_df")
            if fo_cisd_df is not None and not fo_cisd_df.empty:
                st.dataframe(_style_dataframe(fo_cisd_df.sort_values("Confidence", ascending=False)), use_container_width=True, height=500)
                st.download_button("📥 Download as Excel", data=to_excel_bytes(fo_cisd_df, "F&O CISD"), file_name=f"nse_fo_cisd_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="dl_fo_cisd")
            else:
                st.info("Run an F&O CISD scan above.")
            if st.session_state.get("fo_cisd_errors"):
                with st.expander(f"⚠️ Skipped ({len(st.session_state['fo_cisd_errors'])})"):
                    st.text("\n".join(st.session_state["fo_cisd_errors"][:20]))

    with tab_golden_death:
        gd_lim = st.number_input("Limit (0=all)", min_value=0, max_value=len(symbols), value=min(300, len(symbols)), step=50, key="gd_limit")
        gd_universe = symbols if gd_lim == 0 else symbols[:gd_lim]
        if st.button(f"✝️ Run Golden/Death Cross Scan ({len(gd_universe)} symbols)", key="gd_run"):
            with st.spinner("Scanning…"):
                gd_results, gd_errors, gd_stats = run_golden_death_cross_scan(fyers, gd_universe)
                st.session_state["golden_death_df"] = pd.DataFrame(gd_results); st.session_state["golden_death_errors"] = gd_errors; st.session_state["golden_death_stats"] = gd_stats
        if "golden_death_stats" in st.session_state:
            _display_scan_summary(st.session_state["golden_death_stats"])
        gd_df = st.session_state.get("golden_death_df")
        if gd_df is not None and not gd_df.empty:
            st.dataframe(_style_dataframe(gd_df.sort_values("Confidence %", ascending=False)), use_container_width=True, height=500)
            st.download_button("📥 Download as Excel", data=to_excel_bytes(gd_df, "Swing Golden-Death"), file_name=f"nse_golden_death_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="dl_gd")
        else:
            st.info("Run a Golden/Death Cross scan above.")
        if st.session_state.get("golden_death_errors"):
            with st.expander(f"⚠️ Skipped ({len(st.session_state['golden_death_errors'])})"):
                st.text("\n".join(st.session_state["golden_death_errors"][:20]))

    with tab_premarket:
        st.caption("⚠️ Buy/Sell Volume is a proxy from last 10 sessions — not live tick data.")
        pm_lim = st.number_input("Limit (0=all)", min_value=0, max_value=len(symbols), value=min(300, len(symbols)), step=50, key="pm_limit")
        pm_universe = symbols if pm_lim == 0 else symbols[:pm_lim]
        if st.button(f"🌅 Run Pre-Market Scan ({len(pm_universe)} symbols)", key="pm_run"):
            with st.spinner("Scanning…"):
                pm_results, pm_errors, pm_stats = run_premarket_scan(fyers, pm_universe)
                st.session_state["premarket_df"] = pd.DataFrame(pm_results); st.session_state["premarket_errors"] = pm_errors; st.session_state["premarket_stats"] = pm_stats
        if "premarket_stats" in st.session_state:
            _display_scan_summary(st.session_state["premarket_stats"])
        pm_df = st.session_state.get("premarket_df")
        if pm_df is not None and not pm_df.empty:
            pm_filter = st.selectbox("Filter", ["All", "Bullish Candidates", "Bearish Candidates", "High RVOL", "Gap Up", "Gap Down"], key="pm_filter")
            pm_view = pm_df.copy()
            try:
                if pm_filter == "Bullish Candidates":
                    pm_view = pm_view[pm_view["Expected Opening Trend"].str.contains("Bullish", na=False)]
                elif pm_filter == "Bearish Candidates":
                    pm_view = pm_view[pm_view["Expected Opening Trend"].str.contains("Bearish", na=False)]
                elif pm_filter == "High RVOL":
                    pm_view = pm_view[pm_view["RVOL"].str.contains("❤️|🔥", na=False, regex=True)]
                elif pm_filter == "Gap Up":
                    pm_view = pm_view[pm_view["Gap %"].str.replace("%", "", regex=False).astype(float) > 0]
                elif pm_filter == "Gap Down":
                    pm_view = pm_view[pm_view["Gap %"].str.replace("%", "", regex=False).astype(float) < 0]
            except Exception:
                pm_view = pm_df.copy()
            st.dataframe(_style_dataframe(pm_view.sort_values("AI Score", ascending=False)), use_container_width=True, height=500)
            st.download_button("📥 Download as Excel", data=to_excel_bytes(pm_view, "Pre-Market"), file_name=f"nse_premarket_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="dl_pm")
        else:
            st.info("Run a Pre-Market scan above.")
        if st.session_state.get("premarket_errors"):
            with st.expander(f"⚠️ Skipped ({len(st.session_state['premarket_errors'])})"):
                st.text("\n".join(st.session_state["premarket_errors"][:20]))

    with tab_fo_15m_cisd:
        fo15_symbols = load_nse_fo_stock_symbols()
        st.caption(f"Loaded {len(fo15_symbols)} F&O-permitted NSE stocks.")
        if not fo15_symbols:
            st.warning("No F&O symbols loaded.")
        else:
            fo15_lim = st.number_input("Limit (0=all)", min_value=0, max_value=len(fo15_symbols), value=len(fo15_symbols), step=25, key="fo15_limit")
            fo15_universe = fo15_symbols if fo15_lim == 0 else fo15_symbols[:fo15_lim]
            if st.button(f"🎯 Run F&O 15-Min CISD Scan ({len(fo15_universe)} symbols)", key="fo15_run"):
                with st.spinner("Scanning…"):
                    fo15_results, fo15_errors, fo15_stats = run_fo_15min_cisd_scan(fyers, fo15_universe)
                    st.session_state["fo15_cisd_df"] = pd.DataFrame(fo15_results); st.session_state["fo15_cisd_errors"] = fo15_errors; st.session_state["fo15_cisd_stats"] = fo15_stats
            if "fo15_cisd_stats" in st.session_state:
                _display_scan_summary(st.session_state["fo15_cisd_stats"])
            fo15_df = st.session_state.get("fo15_cisd_df")
            if fo15_df is not None and not fo15_df.empty:
                st.dataframe(_style_dataframe(fo15_df.sort_values("Confidence %", ascending=False)), use_container_width=True, height=500)
                st.download_button("📥 Download as Excel", data=to_excel_bytes(fo15_df, "F&O 15-Min CISD"), file_name=f"nse_fo_15min_cisd_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="dl_fo15")
            else:
                st.info("Run an F&O 15-Min CISD scan above.")
            if st.session_state.get("fo15_cisd_errors"):
                with st.expander(f"⚠️ Skipped ({len(st.session_state['fo15_cisd_errors'])})"):
                    st.text("\n".join(st.session_state["fo15_cisd_errors"][:20]))

    with tab_live_ob:
        st.caption("Live 15-minute Order Block BUY/SELL signal engine.")
        lob_col1, lob_col2 = st.columns([2, 1])
        with lob_col1:
            lob_lim = st.number_input("Limit symbols (0=ALL)", min_value=0, max_value=len(symbols), value=0, step=50, key="live_ob_limit")
            lob_watchlist = symbols if lob_lim == 0 else symbols[:lob_lim]
            st.caption(f"Monitoring {len(lob_watchlist)} stocks.")
        with lob_col2:
            lob_auto = st.checkbox(f"🔁 Auto-refresh every {LIVE_OB_AUTO_REFRESH_SECONDS}s", value=False, key="live_ob_auto_refresh")
        run_lob_now = st.button(f"🔔 Run Live OB Scan ({len(lob_watchlist)} symbols)", key="live_ob_run")
        if run_lob_now or lob_auto:
            if not lob_watchlist:
                st.warning("No symbols available.")
            else:
                seen_keys = _load_seen_signal_keys()
                with st.spinner("Scanning for live 15-min OB signals…"):
                    lob_rows, lob_new_rows, lob_errors, lob_stats, updated_keys = run_live_ob_signal_scan(fyers, lob_watchlist, seen_keys)
                    if lob_new_rows:
                        _persist_new_live_ob_rows(fyers, lob_new_rows)
                st.session_state["live_ob_df"] = pd.DataFrame([{k: v for k, v in r.items() if k not in ("dedup_key", "Symbol")} for r in lob_rows])
                st.session_state["live_ob_errors"] = lob_errors; st.session_state["live_ob_stats"] = lob_stats
                st.session_state["live_ob_last_run"] = _now_ist().strftime("%d-%b-%Y %H:%M:%S")
                for new_row in lob_new_rows:
                    msg = f"{new_row['Signal']} {new_row['Stock']} @ {new_row['Entry']} (SL {new_row['Stop Loss']}, T1 {new_row['Target 1']}, T2 {new_row['Target 2']}, RR {new_row['Risk:Reward']}) — {new_row['Signal Date']} {new_row['Signal Time']}"
                    (st.success if new_row["Direction"] == "BUY" else st.error)(f"{'🟢 NEW BUY' if new_row['Direction'] == 'BUY' else '🔴 NEW SELL'} SIGNAL: {msg}")
                    try:
                        st.toast(msg, icon="🔔")
                    except Exception:
                        pass
        if "live_ob_stats" in st.session_state:
            _display_scan_summary(st.session_state["live_ob_stats"])
        if st.session_state.get("live_ob_last_run"):
            st.caption(f"Last scanned: {st.session_state['live_ob_last_run']} IST")
        lob_df = st.session_state.get("live_ob_df")
        if lob_df is not None and not lob_df.empty:
            st.dataframe(_style_dataframe(lob_df.sort_values("Signal Date", ascending=False)), use_container_width=True, height=450)
            dl1, dl2, dl3 = st.columns(3)
            with dl1:
                st.download_button("📥 Download Live OB (Excel)", data=to_excel_bytes(lob_df, "Live OB Signals"), file_name=f"live_ob_signals_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="dl_live_ob_xlsx")
            with dl2:
                if os.path.exists(_LIVE_OB_MASTER_CSV):
                    with open(_LIVE_OB_MASTER_CSV, "rb") as f:
                        st.download_button("📥 All-Time Log (CSV)", data=f.read(), file_name="live_ob_signals_all_time.csv", mime="text/csv", key="dl_live_ob_csv")
            with dl3:
                if os.path.exists(_LIVE_OB_MASTER_JSON):
                    with open(_LIVE_OB_MASTER_JSON, "rb") as f:
                        st.download_button("📥 All-Time Log (JSON)", data=f.read(), file_name="live_ob_signals_all_time.json", mime="application/json", key="dl_live_ob_json")
        else:
            st.info("Run a Live OB scan above or enable auto-refresh.")
        if st.session_state.get("live_ob_errors"):
            with st.expander(f"⚠️ Skipped ({len(st.session_state['live_ob_errors'])})"):
                st.text("\n".join(st.session_state["live_ob_errors"][:20]))
        if lob_auto:
            time.sleep(LIVE_OB_AUTO_REFRESH_SECONDS); st.rerun()

    with tab_ema_swing:
        st.caption("EMA 50/200 on 4H candles. BUY/SELL requires RSI+MACD+Volume+VWAP confirmation; otherwise 🟡 WATCH.")
        es_lim = st.number_input("Limit (0=all)", min_value=0, max_value=len(symbols), value=min(300, len(symbols)), step=50, key="ema_swing_limit")
        es_universe = symbols if es_lim == 0 else symbols[:es_lim]
        if st.button(f"🌟 Run EMA 50/200 Swing Scan ({len(es_universe)} symbols, 4H)", key="ema_swing_run"):
            with st.spinner("Scanning 4H candles…"):
                es_results, es_errors, es_stats = run_ema_swing_scan(fyers, es_universe)
                st.session_state["ema_swing_df"] = pd.DataFrame(es_results); st.session_state["ema_swing_errors"] = es_errors; st.session_state["ema_swing_stats"] = es_stats
        if "ema_swing_stats" in st.session_state:
            _display_scan_summary(st.session_state["ema_swing_stats"])
        es_df = st.session_state.get("ema_swing_df")
        if es_df is not None and not es_df.empty:
            es_filter = st.selectbox("Filter", ["All", "BUY only", "SELL only", "WATCH only"], key="ema_swing_filter")
            es_view = es_df.copy()
            try:
                if es_filter == "BUY only":
                    es_view = es_view[es_view["Trade Decision"].str.contains("BUY", na=False)]
                elif es_filter == "SELL only":
                    es_view = es_view[es_view["Trade Decision"].str.contains("SELL", na=False)]
                elif es_filter == "WATCH only":
                    es_view = es_view[es_view["Trade Decision"].str.contains("WATCH", na=False)]
            except Exception:
                pass
            es_sorted = es_view.sort_values("AI Score", ascending=False)
            st.dataframe(_style_dataframe(es_sorted), use_container_width=True, height=500)
            ec1, ec2, ec3 = st.columns(3)
            with ec1:
                st.download_button("📥 Excel", data=to_excel_bytes(es_sorted, "EMA Swing 4H"), file_name=f"nse_ema_swing_4h_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="dl_ema_swing_xlsx")
            with ec2:
                st.download_button("📥 CSV", data=to_csv_bytes(es_sorted), file_name=f"nse_ema_swing_4h_{_now_ist().strftime('%Y%m%d_%H%M')}.csv", mime="text/csv", key="dl_ema_swing_csv")
            with ec3:
                st.download_button("📥 JSON", data=to_json_bytes(es_sorted), file_name=f"nse_ema_swing_4h_{_now_ist().strftime('%Y%m%d_%H%M')}.json", mime="application/json", key="dl_ema_swing_json")
        else:
            st.info("Run an EMA 50/200 Swing scan above.")
        if st.session_state.get("ema_swing_errors"):
            with st.expander(f"⚠️ Skipped ({len(st.session_state['ema_swing_errors'])})"):
                st.text("\n".join(st.session_state["ema_swing_errors"][:20]))

    with tab_institutional:
        st.markdown(
            "### 🏆 Institutional-Quality Signal Engine\n"
            "**20-Point Validation:** HTF Trend · BOS · CHOCH · CISD · OB Quality · "
            "Fresh OB · Untested OB · Liquidity Sweep · FVG · Volume · Candle · ATR · "
            "Momentum · VWAP · EMA · RSI · MACD · ADX · RR ≥ 1:2  \n"
            "Signals with **AI Confidence < 80%** are automatically rejected.  \n"
            "**Grades:** 🥇 A+ (20/20, ≥93%) · 🥈 A (≥17, ≥88%) · 🥉 B (≥14, ≥80%) · C (≥10, ≥70%) · ⬛ REJECT  \n"
            "This tab's report also includes the full **Institutional Price Action engine** "
            "columns (Swing Structure, BOS/CHOCH Status, Breakout Status, Liquidity Sweep, "
            "Order Block, FVG, Support/Resistance, Trend Strength, Pullback Quality, "
            "Price Action Score/Confidence, Decision, Reject Reason, Institutional Grade)."
        )
        st.divider()

        inst_c1, inst_c2, inst_c3, inst_c4 = st.columns([1, 1, 1, 2])
        with inst_c1:
            inst_lim = st.number_input("Limit symbols (0 = all)", min_value=0, max_value=len(symbols), value=min(200, len(symbols)), step=50, key="inst_limit")
        with inst_c2:
            inst_xgb = st.checkbox("Enable XGBoost ML", value=False, key="inst_xgb", disabled=not XGBOOST_AVAILABLE)
        with inst_c3:
            inst_gf = st.selectbox("Grade Filter", ["All (A+ to C)", "A+ only", "A+ and A", "A+ A B"], key="inst_grade_filter")
        with inst_c4:
            inst_show_reports = st.checkbox("📋 Show per-signal AI Reports below table", value=True, key="inst_show_reports")

        inst_universe = symbols if inst_lim == 0 else symbols[:inst_lim]

        if st.button(f"🏆 Run Institutional Scan ({len(inst_universe)} symbols)", key="inst_run"):
            with st.spinner("Fetching NIFTY benchmark…"):
                inst_nifty = fetch_nifty_benchmark(fyers)
            with st.spinner(f"Running 20-point validation on {len(inst_universe)} symbols…"):
                inst_results, inst_errors, inst_stats = run_scan_enhanced(fyers, inst_universe, inst_nifty, inst_xgb)
                inst_full_df = pd.DataFrame(inst_results)
                inst_dc = [c for c in inst_full_df.columns if not c.startswith("_")]
                inst_scan_df = inst_full_df[inst_dc] if not inst_full_df.empty else inst_full_df
                if not inst_full_df.empty and "_Enhanced_Pass" in inst_full_df.columns:
                    inst_strict_df = inst_full_df[inst_full_df["_Enhanced_Pass"] == True]
                    inst_strict_dc = [c for c in inst_strict_df.columns if not c.startswith("_")]
                    inst_strict_df = inst_strict_df[inst_strict_dc]
                else:
                    inst_strict_df = inst_scan_df
            st.session_state["inst_scan_df"] = inst_scan_df
            st.session_state["inst_strict_df"] = inst_strict_df
            st.session_state["inst_errors"] = inst_errors
            st.session_state["inst_stats"] = inst_stats
            del inst_full_df
            gc.collect()

        if "inst_stats" in st.session_state:
            _display_scan_summary(st.session_state["inst_stats"])

        inst_df = st.session_state.get("inst_scan_df")

        if inst_df is not None:
            if inst_df.empty:
                st.warning(
                    "⚠️ Scan completed but **0 signals** passed the 80% confidence filter.  \n"
                    "💡 Try: increase symbol limit, or use the **'Show All (including low confidence)'** toggle below."
                )
            else:
                total_found = len(inst_df)
                grade_counts = {}
                if "Signal Grade" in inst_df.columns:
                    grade_counts = inst_df["Signal Grade"].value_counts().to_dict()
                grade_str = " · ".join(f"{g}: {c}" for g, c in sorted(grade_counts.items()))
                st.success(f"✅ **{total_found} signals found** after validation  |  {grade_str if grade_str else 'Grade data unavailable'}")

        inst_show_all = st.checkbox(
            "🔓 Show ALL signals (bypass 80% confidence filter)",
            value=True, key="inst_show_all",
            help="When ON: shows every signal the scan produced regardless of confidence. "
                 "When OFF: only signals that passed the strict 80% threshold are shown."
        )

        if inst_df is not None and not inst_df.empty:
            if inst_show_all:
                view_df = inst_df.copy()
            else:
                strict_df = st.session_state.get("inst_strict_df", inst_df)
                view_df = strict_df.copy() if strict_df is not None and not strict_df.empty else inst_df.copy()

            try:
                if inst_gf == "A+ only":
                    filtered = view_df[view_df["Signal Grade"] == "A+"]
                elif inst_gf == "A+ and A":
                    filtered = view_df[view_df["Signal Grade"].isin(["A+", "A"])]
                elif inst_gf == "A+ A B":
                    filtered = view_df[view_df["Signal Grade"].isin(["A+", "A", "B"])]
                else:
                    filtered = view_df
                if not filtered.empty:
                    view_df = filtered
                else:
                    st.warning(f"⚠️ Grade filter **'{inst_gf}'** returned 0 rows — showing all grades instead.  \nChange the Grade Filter dropdown to see specific grades.")
            except (KeyError, TypeError):
                pass

            if view_df.empty:
                st.warning("⚠️ No signals to display.  \n💡 Enable **'Show ALL signals'** toggle above, or increase the symbol limit.")
            else:
                view_sorted = view_df.sort_values("AI Confidence %", ascending=False).reset_index(drop=True)

                st.markdown("#### 📊 Scan Summary")
                mk1, mk2, mk3, mk4, mk5, mk6 = st.columns(6)
                mk1.metric("✅ Total Signals", len(view_sorted))
                try:
                    mk2.metric("🥇 A+ Grades", int((view_sorted["Signal Grade"] == "A+").sum()))
                except Exception:
                    mk2.metric("🥇 A+ Grades", "—")
                try:
                    mk3.metric("🟢 BUY Signals", int(view_sorted["Enhanced Signal"].str.contains("BUY", na=False).sum()))
                except Exception:
                    mk3.metric("🟢 BUY Signals", "—")
                try:
                    mk4.metric("🔴 SELL Signals", int(view_sorted["Enhanced Signal"].str.contains("SELL", na=False).sum()))
                except Exception:
                    mk4.metric("🔴 SELL Signals", "—")
                try:
                    mk5.metric("📈 Avg Confidence", f"{view_sorted['AI Confidence %'].mean():.1f}%")
                except Exception:
                    mk5.metric("📈 Avg Confidence", "—")
                try:
                    mk6.metric("🎯 Avg Confirmations", f"{view_sorted['Confirmations Passed'].mean():.1f}/20")
                except Exception:
                    mk6.metric("🎯 Avg Confirmations", "—")

                st.divider()
                st.markdown("#### 📋 Signal Table")

                priority_cols = [
                    "Signal Date", "Signal Time", "Stock", "LTP",
                    "Enhanced Decision", "Enhanced Signal", "Signal Grade", "AI Confidence %",
                    "Confirmations Passed", "Confirmations Failed",
                    "Enhanced Entry", "Enhanced SL",
                    "Enhanced Target 1", "Enhanced Target 2", "Enhanced Target 3", "Enhanced RR",
                    "HTF Trend", "SMC Structure", "CISD",
                    "OB Type (Bullish)", "OB Type (Bearish)", "Order Block Zone", "Order Block Strength",
                    "FVG", "FVG Freshness", "FVG Filled %", "FVG Gap Size", "FVG Nearest Distance",
                    "Liquidity Sweep", "ADX", "+DI", "-DI", "Momentum",
                    "RSI", "MACD Signal", "Supertrend", "VWAP", "RVOL",
                    "MTF Trend", "RS vs NIFTY", "AI Score",
                    "XGBoost Trend", "XGBoost Confidence (%)",
                    "Swing Structure", "BOS Status", "CHOCH Status", "Trend Strength",
                    "Pullback Quality", "Price Action Score", "Price Action Confidence %",
                    "Decision", "Institutional Grade", "Reject Reason",
                    "Signal Reason",
                ]
                _exclude = {"AI Report", "Signal Reason"}
                existing_priority = [c for c in priority_cols if c in view_sorted.columns and c not in _exclude]
                remaining = [c for c in view_sorted.columns if c not in existing_priority and c not in _exclude]
                table_cols = existing_priority + remaining

                table_df = view_sorted[table_cols].copy()
                for _col in table_df.columns:
                    if table_df[_col].dtype == object:
                        table_df[_col] = table_df[_col].fillna("—").astype(str)
                    else:
                        table_df[_col] = table_df[_col].fillna(0)

                try:
                    st.dataframe(_style_dataframe(table_df), use_container_width=True, height=500)
                except Exception:
                    st.dataframe(table_df, use_container_width=True, height=500)

                st.markdown("#### 💾 Export")
                idl1, idl2, idl3 = st.columns(3)
                with idl1:
                    st.download_button("📥 Download as Excel", data=to_excel_bytes(view_sorted, "Institutional Signals"), file_name=f"nse_institutional_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="dl_inst_xlsx")
                with idl2:
                    st.download_button("📥 Download as CSV", data=to_csv_bytes(view_sorted), file_name=f"nse_institutional_{_now_ist().strftime('%Y%m%d_%H%M')}.csv", mime="text/csv", key="dl_inst_csv")
                with idl3:
                    st.download_button("📥 Download as JSON", data=to_json_bytes(view_sorted), file_name=f"nse_institutional_{_now_ist().strftime('%Y%m%d_%H%M')}.json", mime="application/json", key="dl_inst_json")

                if inst_show_reports:
                    st.divider()
                    st.markdown(f"#### 🧠 AI Signal Reports — {len(view_sorted)} Signal(s)")
                    st.caption("Each card shows the full institutional analysis for that signal.")

                    for idx, row in view_sorted.iterrows():
                        stock = str(row.get("Stock", "?"))
                        decision = str(row.get("Enhanced Decision", "—"))
                        grade = str(row.get("Signal Grade", "?"))
                        confidence = row.get("AI Confidence %", 0)
                        passed = row.get("Confirmations Passed", "?")
                        failed_str = str(row.get("Confirmations Failed", "None"))
                        ltp = row.get("LTP", "—")
                        entry = row.get("Enhanced Entry", "—")
                        sl = row.get("Enhanced SL", "—")
                        t1 = row.get("Enhanced Target 1", "—")
                        t2 = row.get("Enhanced Target 2", "—")
                        t3 = row.get("Enhanced Target 3", "—")
                        rr = row.get("Enhanced RR", "—")
                        sig_date = row.get("Signal Date", "—")
                        sig_time = row.get("Signal Time", "—")
                        reason_raw = str(row.get("Signal Reason", "—"))
                        ai_report = str(row.get("AI Report", ""))
                        htf = str(row.get("HTF Trend", "—"))
                        fvg_lbl = str(row.get("FVG", "—"))
                        liq = str(row.get("Liquidity Sweep", "—"))
                        ob_bull = str(row.get("OB Type (Bullish)", "—"))
                        ob_bear = str(row.get("OB Type (Bearish)", "—"))
                        adx_val = row.get("ADX", "—")
                        momentum = str(row.get("Momentum", "—"))
                        pa_decision = str(row.get("Decision", "—"))
                        pa_grade = str(row.get("Institutional Grade", "—"))
                        pa_reject = str(row.get("Reject Reason", "—"))

                        grade_color = {"A+": "#006100", "A": "#1a7a1a", "B": "#ff8c00", "C": "#cc6600", "REJECT": "#888888"}.get(grade, "#333333")
                        is_buy = "BUY" in decision

                        st.markdown(
                            f"""
<div style="border:2px solid {'#006100' if is_buy else '#9C0006'};border-radius:10px;padding:16px;margin-bottom:20px;background:{'#f0fff0' if is_buy else '#fff0f0'}">
<h3 style="margin:0 0 4px 0;color:{'#006100' if is_buy else '#9C0006'}">
{'🟢' if is_buy else '🔴'} {stock} &nbsp;|&nbsp; {decision}
</h3>
<span style="background:{grade_color};color:#fff;padding:3px 10px;border-radius:4px;font-weight:bold;font-size:14px">Grade: {grade}</span>
&nbsp;&nbsp;
<span style="background:#1a1a2e;color:#fff;padding:3px 10px;border-radius:4px;font-size:14px">Confidence: {confidence}%</span>
&nbsp;&nbsp;
<span style="background:#4a148c;color:#fff;padding:3px 10px;border-radius:4px;font-size:14px">PA Decision: {pa_decision} (Inst. Grade {pa_grade})</span>
&nbsp;&nbsp;
<span style="color:#555;font-size:13px">✅ {passed}/20 confirmations &nbsp;|&nbsp; 📅 {sig_date} {sig_time}</span>
</div>
""",
                            unsafe_allow_html=True,
                        )

                        col_a, col_b, col_c, col_d = st.columns(4)
                        col_a.metric("LTP", f"₹{ltp}")
                        col_a.metric("Entry", f"₹{entry}")
                        col_b.metric("Stop Loss 🔴", f"₹{sl}")
                        col_b.metric("R:R", f"1:{rr}")
                        col_c.metric("Target 1 🎯", f"₹{t1}")
                        col_c.metric("Target 2 🎯", f"₹{t2}")
                        col_d.metric("Target 3 🚀", f"₹{t3}")
                        col_d.metric("ADX", f"{adx_val}")

                        ctx1, ctx2, ctx3, ctx4 = st.columns(4)
                        ctx1.info(f"**HTF Trend**\n\n{htf}")
                        ctx2.info(f"**FVG**\n\n{fvg_lbl}")
                        ctx3.info(f"**Liquidity**\n\n{liq}")
                        ctx4.info(f"**Momentum**\n\n{momentum}")

                        ob1, ob2 = st.columns(2)
                        ob1.success(f"**Bullish OB:** {ob_bull}") if "Fresh" in ob_bull or "Institutional" in ob_bull else ob1.warning(f"**Bullish OB:** {ob_bull}")
                        ob2.error(f"**Bearish OB:** {ob_bear}") if "Fresh" in ob_bear or "Institutional" in ob_bear else ob2.warning(f"**Bearish OB:** {ob_bear}")

                        if pa_decision in ("⏸️ WAIT", "🚫 NO TRADE"):
                            st.warning(f"⏸️ **Institutional Price Action Engine:** {pa_decision} — {pa_reject}")

                        st.markdown("**✅ Why this signal was generated:**")
                        reasons = [r.strip() for r in reason_raw.split(" | ") if r.strip() and r.strip() != "—"]
                        if reasons:
                            reason_cols = st.columns(min(len(reasons), 3))
                            for i, reason in enumerate(reasons):
                                reason_cols[i % 3].success(f"✓ {reason}")
                        else:
                            st.caption("No specific reasons recorded.")

                        if failed_str and failed_str.lower() not in ("none", "—", ""):
                            with st.expander(f"❌ Failed confirmations ({failed_str.count(',') + 1 if ',' in failed_str else 1})"):
                                for f_item in failed_str.split(","):
                                    f_item = f_item.strip()
                                    if f_item:
                                        st.markdown(f"- ❌ {f_item}")

                        st.markdown("**📄 Full AI Analysis Report:**")
                        if ai_report and ai_report not in ("—", ""):
                            report_lines = [ln.strip() for ln in ai_report.split(" | ") if ln.strip()]
                            rep_df_data = []
                            for line in report_lines:
                                if ":" in line:
                                    key_part, val_part = line.split(":", 1)
                                    val_clean = val_part.strip()
                                    if any(x in val_clean for x in ["✓", "Bullish", "High ✓", "Institutional", "Trending ✓", "Strong", "Above ✓", "Low"]):
                                        status = "🟢"
                                    elif any(x in val_clean for x in ["✗", "Bearish", "Low ✗", "Sideways", "Retail", "High Risk", "Below", "High ✗"]):
                                        status = "🔴"
                                    else:
                                        status = "🟡"
                                    rep_df_data.append({"": status, "Metric": key_part.strip(), "Value": val_clean})
                            if rep_df_data:
                                rep_df = pd.DataFrame(rep_df_data)
                                st.dataframe(rep_df, use_container_width=True, hide_index=True, height=min(38 * len(rep_df_data) + 40, 560))
                            else:
                                for ln in report_lines:
                                    st.markdown(f"- {ln}")
                        else:
                            st.info("AI Report not available for this signal.")

                        st.divider()

        elif "inst_scan_df" in st.session_state:
            st.info(
                "ℹ️ Scan ran but returned 0 signals after the institutional filter.  \n"
                "**Enable the 'Show ALL signals' toggle above** — that lets you see every stock "
                "the scan analysed, even those that didn't pass the 80% confidence bar.  \n"
                "You can also increase the symbol limit to scan more stocks."
            )
        else:
            st.info(
                "👆 Click **'Run Institutional Scan'** above to start.  \n"
                "The scanner runs the full 20-point institutional validation on every symbol "
                "and shows each signal with a complete AI Report card below the table."
            )

        if st.session_state.get("inst_errors"):
            with st.expander(f"⚠️ Skipped/failed symbols ({len(st.session_state['inst_errors'])})"):
                st.caption("Most stocks are skipped for missing/invalid data, not app errors.")
                st.text("\n".join(st.session_state["inst_errors"][:20]))

    with tab_fo_oi:
        _fo_oi_symbols = load_nse_fo_stock_symbols()
        _show_fo_oi_tab(fyers, _fo_oi_symbols)

    if st.session_state.get("scan_errors"):
        with st.expander(f"⚠️ Skipped/failed symbols ({len(st.session_state['scan_errors'])})"):
            st.text("\n".join(st.session_state["scan_errors"][:20]))

    gc.collect()


# Pass your Fyers object here:
# show_scanner(fyers)
# ════════════════════════════════════════════════════════════════════════
# VOLUME TOP / BOTTOM IDENTIFIER  —  additive module
# ────────────────────────────────────────────────────────────────────────
# Paste this whole block into your main scanner .py file (anywhere below
# the existing helper functions such as calculate_rsi / calculate_atr /
# _validate_symbols / ScanStats / _safe_history, since it reuses them).
#
# It does NOT modify or remove anything you already have. It adds:
#   1. _classify_volume_top_bottom(df)   -> pure detection logic
#   2. _fetch_volume_top_bottom(...)     -> per-symbol worker (threaded)
#   3. run_volume_top_bottom_scan(...)   -> batch scan (same pattern as
#                                            your other run_* functions)
#   4. A ready-made Streamlit tab block (`tab_vol_tb`) to drop into
#      show_scanner(), with two separate tables: TOP list and BOTTOM list,
#      plus Excel/CSV/JSON export.
#
# WHAT IT DETECTS
# ────────────────
# On the most recently CLOSED daily candle for each stock:
#   • RVOL = today's volume ÷ 20-day average volume
#   • Only candles with RVOL >= 2.0x (configurable) are considered
#   • "Volume Top"    -> close is within 2% of the 20-day high AND shows a
#                         bearish tell (red candle / long upper wick / RSI>=68)
#                         => possible exhaustion / distribution top
#   • "Volume Bottom" -> close is within 2% of the 20-day low AND shows a
#                         bullish tell (green candle / long lower wick / RSI<=32)
#                         => possible capitulation / accumulation bottom
# ════════════════════════════════════════════════════════════════════════

VOL_TB_LOOKBACK = 20          # candles used to define "recent high/low"
VOL_TB_MIN_RVOL = 2.0         # minimum relative volume to qualify at all
VOL_TB_PROXIMITY_PCT = 2.0    # must be within this % of the recent high/low


def _volume_top_reason(last_close, last_open, upper_wick, body, rsi_val) -> str:
    reasons = []
    if last_close < last_open:
        reasons.append("Red candle on the spike")
    if upper_wick > body * 1.2:
        reasons.append("Long upper wick (selling into strength)")
    if rsi_val >= 68:
        reasons.append(f"RSI overbought ({rsi_val:.1f})")
    return ", ".join(reasons) if reasons else "High volume at recent high"


def _volume_bottom_reason(last_close, last_open, lower_wick, body, rsi_val) -> str:
    reasons = []
    if last_close > last_open:
        reasons.append("Green candle on the spike")
    if lower_wick > body * 1.2:
        reasons.append("Long lower wick (buying into weakness)")
    if rsi_val <= 32:
        reasons.append(f"RSI oversold ({rsi_val:.1f})")
    return ", ".join(reasons) if reasons else "High volume at recent low"


def _classify_volume_top_bottom(df) -> dict:
    """
    Classifies the most recently CLOSED candle as a Volume Top, Volume
    Bottom, or Neither. Returns a dict with type = 'TOP' | 'BOTTOM' | 'NONE'.
    Uses the same calculate_rsi() helper already defined in this file.
    """
    if len(df) < VOL_TB_LOOKBACK + 5:
        return {"type": "NONE"}

    last = df.iloc[-1]
    recent = df.tail(VOL_TB_LOOKBACK)
    vol_avg = df["Volume"].tail(VOL_TB_LOOKBACK).mean()
    rvol = float(last["Volume"] / vol_avg) if vol_avg > 0 else 0.0
    if rvol < VOL_TB_MIN_RVOL:
        return {"type": "NONE", "rvol": round(rvol, 2)}

    recent_high = float(recent["High"].max())
    recent_low = float(recent["Low"].min())
    last_close = float(last["Close"])
    last_open = float(last["Open"])
    last_high = float(last["High"])
    last_low = float(last["Low"])

    dist_from_high_pct = ((recent_high - last_close) / recent_high * 100) if recent_high else 100.0
    dist_from_low_pct = ((last_close - recent_low) / recent_low * 100) if recent_low else 100.0

    rsi_val = float(calculate_rsi(df["Close"]).iloc[-1])
    body = abs(last_close - last_open)
    upper_wick = last_high - max(last_close, last_open)
    lower_wick = min(last_close, last_open) - last_low

    # --- Volume Top: exhaustion / distribution near the recent high ---
    if dist_from_high_pct <= VOL_TB_PROXIMITY_PCT:
        bearish_tell = (last_close < last_open) or (upper_wick > body * 1.2) or (rsi_val >= 68)
        if bearish_tell:
            return {
                "type": "TOP",
                "rvol": round(rvol, 2),
                "rsi": round(rsi_val, 1),
                "reference_level": round(recent_high, 2),
                "distance_pct": round(dist_from_high_pct, 2),
                "reason": _volume_top_reason(last_close, last_open, upper_wick, body, rsi_val),
            }

    # --- Volume Bottom: capitulation / accumulation near the recent low ---
    if dist_from_low_pct <= VOL_TB_PROXIMITY_PCT:
        bullish_tell = (last_close > last_open) or (lower_wick > body * 1.2) or (rsi_val <= 32)
        if bullish_tell:
            return {
                "type": "BOTTOM",
                "rvol": round(rvol, 2),
                "rsi": round(rsi_val, 1),
                "reference_level": round(recent_low, 2),
                "distance_pct": round(dist_from_low_pct, 2),
                "reason": _volume_bottom_reason(last_close, last_open, lower_wick, body, rsi_val),
            }

    return {"type": "NONE", "rvol": round(rvol, 2)}


def _fetch_volume_top_bottom(fyers, symbol):
    """Per-symbol worker. Follows the exact same pattern as your other
    _fetch_* functions (uses _safe_history / _VALID_EQ_SYMBOL_RE)."""
    if not isinstance(symbol, str) or not _VALID_EQ_SYMBOL_RE.match(symbol):
        return None, f"{symbol}: invalid symbol format — skipped"
    resp, err = _safe_history(fyers, {
        "symbol": symbol, "resolution": "D", "date_format": "1",
        "range_from": DATE_FROM, "range_to": DATE_TO, "cont_flag": "1",
    })
    if err:
        return None, f"{symbol}: {err}"
    candles = resp.get("candles") if resp else None
    if not candles or len(candles) < VOL_TB_LOOKBACK + 5:
        return None, f"{symbol}: insufficient history"
    try:
        df = pd.DataFrame(candles, columns=["Time", "Open", "High", "Low", "Close", "Volume"])
        df["Time"] = pd.to_datetime(df["Time"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
        df[["Open", "High", "Low", "Close", "Volume"]] = df[["Open", "High", "Low", "Close", "Volume"]].apply(pd.to_numeric, errors="coerce")
        df = df.dropna(subset=["Open", "High", "Low", "Close"])
        if len(df) < VOL_TB_LOOKBACK + 5:
            return None, f"{symbol}: insufficient valid candle data"
    except (KeyError, ValueError, TypeError) as e:
        return None, f"{symbol}: malformed candle data ({e})"

    try:
        result = _classify_volume_top_bottom(df)
        if result["type"] == "NONE":
            return None, None  # not an error, just doesn't qualify

        last_close = float(df["Close"].iloc[-1])
        last_volume = int(df["Volume"].iloc[-1])
        vol_avg = float(df["Volume"].tail(VOL_TB_LOOKBACK).mean())
        stock_ticker = symbol.replace("NSE:", "").replace("-EQ", "")
        signal_date_str, signal_time_str = _candle_signal_timestamp(df, is_daily=True)

        row = {
            "Signal Date": signal_date_str,
            "Signal Time": signal_time_str,
            "Stock": stock_ticker,
            "LTP": round(last_close, 2),
            "Type": "🔴 Volume TOP" if result["type"] == "TOP" else "🟢 Volume BOTTOM",
            "RVOL": _format_rvol_display(result["rvol"]),
            "_RVOL_RAW": result["rvol"],
            "Volume": last_volume,
            "Avg Volume (20d)": int(vol_avg),
            "RSI": result["rsi"],
            f"{VOL_TB_LOOKBACK}D {'High' if result['type'] == 'TOP' else 'Low'}": result["reference_level"],
            "Distance %": result["distance_pct"],
            "Reason": result["reason"],
        }
        return row, None
    except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError, AttributeError) as e:
        return None, f"{symbol}: analysis error ({type(e).__name__})"


def run_volume_top_bottom_scan(fyers, symbols):
    """Threaded batch scan — identical pattern to run_scan() etc."""
    symbols = _validate_symbols(symbols)
    results, errors = [], []
    stats = ScanStats(total=len(symbols))
    progress = st.progress(0.0, text=f"Scanning Volume Top/Bottom 0 / {len(symbols)}")
    done = 0
    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i:i + BATCH_SIZE]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(_fetch_volume_top_bottom, fyers, s): s for s in batch}
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
                progress.progress(done / max(len(symbols), 1), text=f"Scanning Volume Top/Bottom {done} / {len(symbols)}")
        if i + BATCH_SIZE < len(symbols):
            time.sleep(BATCH_PAUSE_SECONDS)
    progress.empty()
    gc.collect()
    return results, errors, stats


# ════════════════════════════════════════════════════════════════════════
# STREAMLIT TAB — paste inside show_scanner()
# ────────────────────────────────────────────────────────────────────────
# Step A) Add a new tab to your st.tabs([...]) call, e.g. rename the
#          existing line to add one more label and unpack one more
#          variable, for example:
#
#   (tab_scanner, tab_intraday, tab_swing, tab_fo, tab_intraday_cisd,
#    tab_fo_cisd, tab_golden_death, tab_premarket, tab_fo_15m_cisd,
#    tab_live_ob, tab_ema_swing, tab_institutional, tab_fo_oi,
#    tab_vol_tb) = st.tabs([
#       "📊 Full Scanner", "⚡ Intraday Scanner", "📈 Swing Trade Scanner",
#       "🏛️ F&O Stocks Scanner", "🕐 Intraday CISD Signals",
#       "🎯 F&O CISD Scanner", "✝️ Swing Trading (Golden/Death Cross)",
#       "🌅 Pre-Market Scanner", "🎯 NSE F&O 15-Min CISD Scanner",
#       "🔔 Live OB Signal Scanner", "🌟 EMA 50/200 Swing (4H)",
#       "🏆 Institutional Scanner", "🔬 F&O OI Analysis",
#       "🌋 Volume Top/Bottom Scanner",
#   ])
#
# Step B) Paste the `with tab_vol_tb:` block below anywhere among the
#          other `with tab_xxx:` blocks in show_scanner().
# ════════════════════════════════════════════════════════════════════════

VOL_TB_TAB_CODE = '''
    with tab_vol_tb:
        st.markdown(
            "### 🌋 Volume Top / Bottom Identifier\\n"
            f"Flags stocks with a volume spike (RVOL ≥ {VOL_TB_MIN_RVOL}x) landing within "
            f"{VOL_TB_PROXIMITY_PCT}% of their {VOL_TB_LOOKBACK}-day high or low, "
            "with a matching bearish/bullish tell — a classic exhaustion-top or "
            "capitulation-bottom volume signature."
        )
        vt_lim = st.number_input("Limit (0=all)", min_value=0, max_value=len(symbols), value=min(300, len(symbols)), step=50, key="vol_tb_limit")
        vt_universe = symbols if vt_lim == 0 else symbols[:vt_lim]
        if st.button(f"🌋 Run Volume Top/Bottom Scan ({len(vt_universe)} symbols)", key="vol_tb_run"):
            with st.spinner("Scanning for volume top/bottom signatures…"):
                vt_results, vt_errors, vt_stats = run_volume_top_bottom_scan(fyers, vt_universe)
                st.session_state["vol_tb_df"] = pd.DataFrame(vt_results)
                st.session_state["vol_tb_errors"] = vt_errors
                st.session_state["vol_tb_stats"] = vt_stats

        if "vol_tb_stats" in st.session_state:
            _display_scan_summary(st.session_state["vol_tb_stats"])

        vt_df = st.session_state.get("vol_tb_df")
        if vt_df is not None and not vt_df.empty:
            display_cols = [c for c in vt_df.columns if not c.startswith("_")]
            top_df = vt_df[vt_df["Type"].str.contains("TOP", na=False)][display_cols].sort_values("RSI", ascending=False)
            bottom_df = vt_df[vt_df["Type"].str.contains("BOTTOM", na=False)][display_cols].sort_values("RSI", ascending=True)

            k1, k2, k3 = st.columns(3)
            k1.metric("Total Signals", len(vt_df))
            k2.metric("🔴 Volume Tops", len(top_df))
            k3.metric("🟢 Volume Bottoms", len(bottom_df))

            st.markdown("#### 🔴 Volume Top Candidates (possible exhaustion / distribution)")
            if not top_df.empty:
                st.dataframe(_style_dataframe(top_df), use_container_width=True, height=350)
            else:
                st.caption("No Volume Top signals found in this scan.")

            st.markdown("#### 🟢 Volume Bottom Candidates (possible capitulation / accumulation)")
            if not bottom_df.empty:
                st.dataframe(_style_dataframe(bottom_df), use_container_width=True, height=350)
            else:
                st.caption("No Volume Bottom signals found in this scan.")

            st.markdown("#### 💾 Export")
            ts = _now_ist().strftime("%Y%m%d_%H%M")
            e1, e2, e3 = st.columns(3)
            with e1:
                st.download_button(
                    "📥 Download Full List (Excel)",
                    data=to_excel_bytes_multi({"Volume Tops": top_df, "Volume Bottoms": bottom_df}),
                    file_name=f"volume_top_bottom_{ts}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    key="dl_vol_tb_xlsx",
                )
            with e2:
                st.download_button("📥 CSV", data=to_csv_bytes(vt_df[display_cols]), file_name=f"volume_top_bottom_{ts}.csv", mime="text/csv", key="dl_vol_tb_csv")
            with e3:
                st.download_button("📥 JSON", data=to_json_bytes(vt_df[display_cols]), file_name=f"volume_top_bottom_{ts}.json", mime="application/json", key="dl_vol_tb_json")
        else:
            st.info("Run a Volume Top/Bottom scan above.")

        if st.session_state.get("vol_tb_errors"):
            with st.expander(f"⚠️ Skipped ({len(st.session_state['vol_tb_errors'])})"):
                st.text("\\n".join(st.session_state["vol_tb_errors"][:20]))
'''
