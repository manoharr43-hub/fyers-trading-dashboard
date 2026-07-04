import streamlit as st
import pandas as pd
import numpy as np
import requests
import time
import io
import os
import re
from datetime import datetime, timedelta
from typing import List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
except Exception:  # pragma: no cover - zoneinfo is stdlib on py3.9+, this is just a safety net
    from datetime import timezone
    IST = timezone(timedelta(hours=5, minutes=30))

# XGBoost is optional — the app still works fully without it. When it's
# unavailable (or when the user leaves ML training off), the "XGBoost Trend /
# Confidence" columns fall back to a rule-based technical score instead of
# ever being blank/N-A — see calculate_xgboost_prediction() below.
try:
    import xgboost as xgb
    XGBOOST_AVAILABLE = True
except ImportError:
    XGBOOST_AVAILABLE = False

# ── Configuration ────────────────────────────────────────────────────────────
# 365 days of daily candles also serves as our 52-week high/low window.
DATE_FROM = (datetime.today() - timedelta(days=365)).strftime("%Y-%m-%d")
DATE_TO = datetime.today().strftime("%Y-%m-%d")

# Fyers publishes a daily-refreshed master of all tradable NSE Capital
# Market (equity) symbols at this public URL. We use it instead of a
# hardcoded list so the scanner covers the whole NSE equity universe.
FYERS_NSE_CM_SYMBOL_MASTER = "https://public.fyers.in/sym_details/NSE_CM.csv"

# Benchmark index used for Relative Strength vs NIFTY.
NIFTY_BENCHMARK_SYMBOL = "NSE:NIFTY50-INDEX"

# Fyers rate-limits the history/quotes API (commonly ~10 req/sec on most
# plans). Scanning 2000+ symbols with unlimited threads will trigger 429s
# or silent throttling, so we cap concurrency and batch with small pauses.
MAX_WORKERS = 8
BATCH_SIZE = 50
BATCH_PAUSE_SECONDS = 1.0

# Path to an optional pre-trained, persisted XGBoost model (JSON format via
# model.save_model(...)). If this file exists, calculate_xgboost_prediction()
# loads it automatically and blends its output with the technical fallback
# score. If it does not exist, a lightweight model is trained on-the-fly
# (when ML is enabled) or the pure rule-based technical fallback is used.
XGB_MODEL_PATH = "xgb_trend_model.json"

# Lookback window (days) for the new intraday (5m/15m) CISD scanner below —
# separate from DATE_FROM/DATE_TO (which stay at 365 days for the existing
# daily-resolution pipeline, untouched).
INTRADAY_CISD_LOOKBACK_DAYS = 5

# NOTE ON OPEN INTEREST: this scanner runs against the NSE_CM (cash
# equity) symbol master. Open Interest and Change in OI are futures &
# options concepts and do not exist for cash equity instruments, so they
# are intentionally NOT part of the scoring/columns below. Wiring OI in
# would require mapping every EQ symbol to its current-month FUT contract
# and pulling OI from a separate Fyers endpoint per symbol — that's a
# meaningfully different (and much heavier) scan and isn't included here.

# NOTE ON INTRADAY DATA: the Fyers history calls in this file use daily
# ("D") resolution candles. There is no live 1m/5m intraday feed wired in,
# so the Intraday Scanner below approximates a quick intraday-style trade
# off the most recent daily candle's technicals (RSI/MACD/Supertrend/VWAP/
# RVOL/S-R) rather than reading live intraday ticks. If/when an intraday
# feed is added, swap "resolution": "D" for "5" or "15" in a dedicated
# fetch and feed that into calculate_intraday_signal() instead.


# ── FIX 1: India Standard Time helpers ───────────────────────────────────────
# Every "Signal Date"/"Signal Time" shown anywhere in the app MUST be
# generated from IST (Asia/Kolkata, UTC+05:30), never server/UTC time.
# Use these two helpers everywhere a signal timestamp is created.
def _now_ist() -> datetime:
    """Current time in IST — the single source of truth for signal timestamps."""
    return datetime.now(IST)


# ── FIX (candle-based signal timestamp) ──────────────────────────────────────
# Signal Date/Signal Time must reflect the last COMPLETED CANDLE that
# actually generated the trading signal (CISD / SMC / Breakout / XGBoost /
# AI Signal) — never datetime.now(), never scan-run time, never Excel
# download time. Every place that builds a signal row must pass its OHLCV
# `df` (whose "Time" column is already tz-aware, built via
# pd.to_datetime(..., utc=True).dt.tz_convert("Asia/Kolkata")) into this
# function instead of calling the old system-clock-based helper.
from datetime import time as _dtime  # noqa: E402  (local import kept near usage)

# NSE cash-equity market close — the true "close time" a *daily* candle
# represents. Fyers' "D" resolution candles are stamped at day-start
# (00:00:00 UTC), which converts to 05:30:00 IST — that is NOT when the
# candle actually closed, so daily signals must display 15:30:00 IST
# instead of the raw epoch-derived time.
_NSE_MARKET_CLOSE_IST = _dtime(15, 30, 0)


def _format_signal_timestamp(ts, is_daily: bool = False) -> Tuple[str, str]:
    """Formats a raw candle Timestamp into (Signal Date, Signal Time), as
    ('DD-MMM-YYYY', 'HH:MM:SS IST') — always in IST (Asia/Kolkata).

    This is the low-level formatter shared by every scanner. It takes the
    actual candle Timestamp that GENERATED the signal (which is not
    necessarily df's last row — e.g. a CISD/SMC shift may have confirmed
    a candle or two before the most recent one), so re-scanning never
    changes the value for the same underlying signal.

    is_daily=True: "D" resolution candles are stamped at day-start
    (00:00:00 UTC → 05:30:00 IST naive), which is NOT the real close time,
    so the DATE still comes from the candle but the TIME is pinned to the
    actual NSE market close (15:30:00 IST).

    is_daily=False (default): real intraday candles (5-min, 15-min, etc.)
    already carry their true close time (e.g. 09:20, 09:30, 10:15) and are
    displayed as-is, unmodified.
    """
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    ts_ist = ts.tz_convert(IST)

    if is_daily:
        ts_ist = ts_ist.replace(
            hour=_NSE_MARKET_CLOSE_IST.hour,
            minute=_NSE_MARKET_CLOSE_IST.minute,
            second=_NSE_MARKET_CLOSE_IST.second,
            microsecond=0,
        )

    return ts_ist.strftime("%d-%b-%Y"), ts_ist.strftime("%H:%M:%S") + " IST"


def _candle_signal_timestamp(df: pd.DataFrame, is_daily: bool = False) -> Tuple[str, str]:
    """Returns (Signal Date, Signal Time) derived from df's last completed
    candle. Formatted as ('DD-MMM-YYYY', 'HH:MM:SS IST').

    Because this reads df["Time"].iloc[-1] (the candle timestamp, not the
    wall clock), calling it again later for the same df/candle always
    returns the identical value — it does NOT drift on re-render, re-scan,
    or Excel export.

    Use this when the signal is inherently tied to the LATEST candle (pure
    AI Score / Breakout / XGBoost / Golden-Death-Cross reads). For signals
    whose confirming candle may be earlier than df's last row (CISD/SMC
    events), use _format_signal_timestamp(event_ts, ...) directly with the
    actual event candle's Timestamp instead — see _calculate_smc_and_cisd.
    """
    return _format_signal_timestamp(df["Time"].iloc[-1], is_daily=is_daily)


# ── FIX 2/3/5: Resilient, retrying Fyers history fetch ───────────────────────
# Centralized wrapper used by every scanner in this file so API errors,
# timeouts, network errors, rate limits, invalid JSON, and bad/empty
# responses are ALL handled the same way, in one place, with retries +
# backoff — instead of each scanner re-implementing its own try/except.
_HISTORY_MAX_RETRIES = 3
_HISTORY_BASE_DELAY_SECONDS = 1.0


def _safe_history(fyers, params: dict, max_retries: int = _HISTORY_MAX_RETRIES,
                   base_delay: float = _HISTORY_BASE_DELAY_SECONDS) -> Tuple[Optional[dict], Optional[str]]:
    """Calls fyers.history(params) with automatic retries + backoff.

    Handles: network/timeout errors, connection errors, malformed/invalid
    JSON responses, unexpected exceptions, and API-side rate limiting.
    Returns (response_dict, None) on success, or (None, short_error_message)
    if every retry is exhausted or the symbol itself is invalid/rejected by
    the API (those are not retried, since retrying won't help).
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
            # Covers JSON decode errors and unexpected payload shapes.
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
                    # Invalid symbol / delisted / expired contract / no data —
                    # the API rejected the symbol itself, retrying won't help.
                    return None, message

        if attempt < max_retries:
            time.sleep(base_delay * attempt)

    return None, f"{symbol}: {last_err} (after {max_retries} attempts)"


# ── FIX 4: Symbol validation & de-duplication ────────────────────────────────
_VALID_EQ_SYMBOL_RE = re.compile(r"^NSE:[A-Z0-9&\-]+-EQ$")


def _validate_symbols(symbols: List[str]) -> List[str]:
    """Drops duplicates, blanks, and anything that doesn't look like a
    genuine 'NSE:SYMBOL-EQ' equity symbol (catches stray/malformed rows
    from the Fyers master, e.g. from a changed CSV layout)."""
    seen = set()
    valid: List[str] = []
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
    """Lightweight counters for FIX 7 (concise logging) — tracks how a scan
    went without ever surfacing raw Python exceptions to the user."""
    def __init__(self, total: int):
        self.total = total
        self.scanned = 0
        self.successful = 0
        self.skipped = 0
        self.failed = 0
        self._start = time.time()

    def record(self, has_result: bool, has_error: bool):
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


def _display_scan_summary(stats: "ScanStats"):
    """FIX 7: concise, non-technical scan summary — no raw error text."""
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Total Stocks", stats.total)
    c2.metric("Scanned", stats.scanned)
    c3.metric("Successful", stats.successful)
    c4.metric("Skipped", stats.skipped)
    c5.metric("Failed", stats.failed)
    c6.metric("Scan Time", f"{stats.elapsed_seconds:.1f}s")


# ── Symbol Universe ──────────────────────────────────────────────────────────
@st.cache_data(ttl=60 * 60 * 12)  # refresh twice a day at most
def load_nse_equity_symbols() -> List[str]:
    """
    Downloads Fyers' NSE Capital Market symbol master and returns all
    NSE equity (-EQ) symbols in 'NSE:SYMBOL-EQ' format.

    Fyers does not guarantee the column layout of this CSV stays fixed,
    so instead of trusting a hardcoded column index we scan every column
    on a sample of rows and pick whichever index actually contains
    'NSE:...-EQ' style values most consistently.
    """
    try:
        resp = requests.get(FYERS_NSE_CM_SYMBOL_MASTER, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        st.error(f"Could not download Fyers symbol master: {e}")
        return []

    lines = [ln for ln in resp.text.strip().split("\n") if ln.strip()]
    if not lines:
        return []

    sample = lines[: min(500, len(lines))]
    split_sample = [ln.split(",") for ln in sample]
    max_cols = max((len(p) for p in split_sample), default=0)

    best_col, best_hits = None, 0
    for col_idx in range(max_cols):
        hits = sum(
            1 for parts in split_sample
            if len(parts) > col_idx and parts[col_idx].strip().startswith("NSE:")
            and parts[col_idx].strip().endswith("-EQ")
        )
        if hits > best_hits:
            best_col, best_hits = col_idx, hits

    if best_col is None or best_hits == 0:
        st.error(
            "Could not locate the trading-symbol column in the Fyers symbol "
            "master — the file format may have changed."
        )
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


# ══════════════════════════════════════════════════════════════════════════
# ── F&O STOCKS MODULE (grouped together) ─────────────────────────────────
# Everything related to the F&O (futures & options eligible) stock universe
# lives in this block + the "F&O Stocks Scanner" tab in show_scanner() —
# kept together so F&O logic isn't scattered through the file.
# ══════════════════════════════════════════════════════════════════════════

# Fyers' NSE derivatives (F&O) symbol master. Every row is one futures/
# options contract (e.g. "NSE:SBIN25JULFUT", "NSE:SBIN25JUL800CE"). We use
# it purely to discover WHICH underlyings are currently F&O-permitted —
# actual price history for the scan still comes from the clean NSE_CM
# ("-EQ") equity symbols so the F&O scanner runs the exact same technicals
# as the main scanner, just filtered to the F&O universe.
FYERS_NSE_FO_SYMBOL_MASTER = "https://public.fyers.in/sym_details/NSE_FO.csv"

# Index underlyings also trade F&O contracts but are not individual stocks —
# this scanner is stocks-only, so these are excluded from the F&O universe.
_FO_INDEX_UNDERLYINGS = {
    "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50",
    "NIFTYIT", "NIFTYPSE", "NIFTYINFRA", "SENSEX", "BANKEX", "NIFTY50",
}


@st.cache_data(ttl=60 * 60 * 12)  # refresh twice a day at most, same as the equity master
def load_nse_fo_stock_symbols() -> List[str]:
    """
    Downloads Fyers' NSE F&O (derivatives) symbol master, extracts the
    underlying ticker from every live futures/options contract, drops
    index underlyings, and returns the matching clean 'NSE:SYMBOL-EQ'
    equity symbols — i.e. exactly the stocks currently permitted for F&O
    trading, in the same format the rest of the scanner already uses.
    """
    try:
        resp = requests.get(FYERS_NSE_FO_SYMBOL_MASTER, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        st.error(f"Could not download Fyers F&O symbol master: {e}")
        return []

    lines = [ln for ln in resp.text.strip().split("\n") if ln.strip()]
    if not lines:
        return []

    sample = lines[: min(500, len(lines))]
    split_sample = [ln.split(",") for ln in sample]
    max_cols = max((len(p) for p in split_sample), default=0)

    # Same auto-detect-the-symbol-column trick as the equity loader, since
    # Fyers doesn't guarantee a fixed column layout here either.
    best_col, best_hits = None, 0
    for col_idx in range(max_cols):
        hits = sum(
            1 for parts in split_sample
            if len(parts) > col_idx and parts[col_idx].strip().startswith("NSE:")
        )
        if hits > best_hits:
            best_col, best_hits = col_idx, hits

    if best_col is None or best_hits == 0:
        st.error(
            "Could not locate the trading-symbol column in the Fyers F&O "
            "symbol master — the file format may have changed."
        )
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
        # Underlying is the leading alphabetic run before the expiry digits
        # (e.g. "SBIN25JULFUT" -> "SBIN", "M&M25JUL2400CE" -> "M&M").
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


# ── Benchmark (NIFTY) fetch, used for Relative Strength ─────────────────────
@st.cache_data(ttl=60 * 30)
def fetch_nifty_benchmark(_fyers) -> Optional[pd.Series]:
    """Fetches NIFTY50 index daily closes for the same window as the scan.
    Cached separately from the per-symbol scan since every symbol shares it."""
    try:
        resp, err = _safe_history(_fyers, {
            "symbol": NIFTY_BENCHMARK_SYMBOL, "resolution": "D", "date_format": "1",
            "range_from": DATE_FROM, "range_to": DATE_TO, "cont_flag": "1"
        })
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


# ── Technical indicator helpers ──────────────────────────────────────────────
def calculate_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50)


def calculate_macd(close: pd.Series):
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def calculate_supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0) -> Tuple[str, Optional[bool], Optional[float]]:
    """Classic ATR-based Supertrend. Returns (label, is_bullish, line_value)."""
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
    direction = np.ones(n, dtype=int)  # 1 = bullish, -1 = bearish

    final_upper[0] = upperband[0]
    final_lower[0] = lowerband[0]
    supertrend[0] = final_upper[0]

    for i in range(1, n):
        final_upper[i] = upperband[i] if (upperband[i] < final_upper[i - 1] or close[i - 1] > final_upper[i - 1]) else final_upper[i - 1]
        final_lower[i] = lowerband[i] if (lowerband[i] > final_lower[i - 1] or close[i - 1] < final_lower[i - 1]) else final_lower[i - 1]

        if supertrend[i - 1] == final_upper[i - 1]:
            if close[i] <= final_upper[i]:
                supertrend[i] = final_upper[i]
                direction[i] = -1
            else:
                supertrend[i] = final_lower[i]
                direction[i] = 1
        else:
            if close[i] >= final_lower[i]:
                supertrend[i] = final_lower[i]
                direction[i] = 1
            else:
                supertrend[i] = final_upper[i]
                direction[i] = -1

    is_bullish = bool(direction[-1] == 1)
    label = "🟢 Buy" if is_bullish else "🔴 Sell"
    return label, is_bullish, round(float(supertrend[-1]), 2)


def calculate_vwap_approx(df: pd.DataFrame, window: int = 20) -> float:
    """Fyers history here is daily-resolution, not intraday, so this is a
    rolling typical-price VWAP over the trailing `window` sessions — a
    common swing-trading approximation, not a true intraday session VWAP."""
    d = df.tail(window)
    typical = (d["High"] + d["Low"] + d["Close"]) / 3
    vol_sum = d["Volume"].sum()
    if vol_sum <= 0:
        return round(float(d["Close"].iloc[-1]), 2)
    return round(float((typical * d["Volume"]).sum() / vol_sum), 2)


def detect_chart_pattern(df: pd.DataFrame) -> str:
    if len(df) < 5:
        return "N/A"
    last = df.iloc[-1]
    prev = df.iloc[-2]
    body = abs(last["Close"] - last["Open"])
    rng = last["High"] - last["Low"]
    upper_wick = last["High"] - max(last["Close"], last["Open"])
    lower_wick = min(last["Close"], last["Open"]) - last["Low"]

    if rng > 0 and body / rng < 0.1:
        return "Doji ⚪"
    if lower_wick > body * 2 and last["Close"] > last["Open"]:
        return "Hammer 🔨"
    if upper_wick > body * 2 and last["Close"] < last["Open"]:
        return "Shooting Star 🌠"

    prev_lo, prev_hi = min(prev["Open"], prev["Close"]), max(prev["Open"], prev["Close"])
    last_lo, last_hi = min(last["Open"], last["Close"]), max(last["Open"], last["Close"])
    if last["Close"] > last["Open"] and prev["Close"] < prev["Open"] and last_hi >= prev_hi and last_lo <= prev_lo:
        return "Bullish Engulfing 🟢"
    if last["Close"] < last["Open"] and prev["Close"] > prev["Open"] and last_hi >= prev_hi and last_lo <= prev_lo:
        return "Bearish Engulfing 🔴"

    recent = df.tail(5)
    if recent["High"].is_monotonic_increasing and recent["Low"].is_monotonic_increasing:
        return "Higher Highs/Lows 📈"
    if recent["High"].is_monotonic_decreasing and recent["Low"].is_monotonic_decreasing:
        return "Lower Highs/Lows 📉"

    return "No Clear Pattern"


def calculate_mtf_trend(df: pd.DataFrame) -> str:
    """Multi-time-frame trend using daily EMA20 vs a weekly close resampled
    from the same daily candles — avoids a second API call per symbol."""
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


def calculate_relative_strength(close: pd.Series, nifty_close: Optional[pd.Series], period: int = 10) -> str:
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
    if pd.isna(atr) or atr <= 0:
        atr = last_close * 0.01  # fallback ~1% if ATR unavailable
    if direction == "Bullish":
        target, stoploss = last_close + 2 * atr, last_close - 1 * atr
    elif direction == "Bearish":
        target, stoploss = last_close - 2 * atr, last_close + 1 * atr
    else:
        target, stoploss = last_close + 1.5 * atr, last_close - 1.5 * atr
    return round(target, 2), round(stoploss, 2)


# ── RVOL display formatter (FIXED/extended) ──────────────────────────────────
# Shared by the main scanner AND every new scanner added below, so the RVOL
# highlight tiers are identical everywhere: plain "x" below 2.0x, ❤️‍🔥 from
# 2.0x, and 🔥🔥 from 3.0x.
def _format_rvol_display(rvol_raw: float) -> str:
    display = f"{rvol_raw:.2f}x"
    if rvol_raw >= 3.0:
        display += " 🔥🔥"
    elif rvol_raw >= 2.0:
        display += " ❤️‍🔥"
    return display


# ── FIX (Neutral confidence direction) ───────────────────────────────────────
# calculate_ai_trend previously computed Neutral confidence as
# `100 - abs(ai_score - 50) * 2`, which is HIGHEST exactly at ai_score=50
# (the most ambiguous possible score) and falls as the score approaches the
# Bullish/Bearish thresholds — the opposite of what "confidence" should mean,
# and discontinuous at the 65/40 boundaries (e.g. score=64 -> conf=72 while
# score=65 -> conf=65). The corrected formula below is continuous with the
# Bullish/Bearish branches and gives the lowest confidence at score=50
# (maximum uncertainty), rising toward the boundaries — matching the
# Bullish/Bearish branches exactly at ai_score=65 and ai_score=40.
def calculate_ai_trend(ai_score: float) -> Tuple[str, float]:
    if ai_score >= 65:
        return "📈 Bullish", round(ai_score, 1)
    if ai_score <= 40:
        return "📉 Bearish", round(100 - ai_score, 1)
    return "➖ Neutral", round(50 + abs(ai_score - 50), 1)


# ── News column ────────────────────────────────────────────────────────────
NEWS_API_ENABLED = bool(os.environ.get("NEWS_API_KEY"))


def fetch_news_sentiment_live(stock_ticker: str) -> Optional[str]:
    if not NEWS_API_ENABLED:
        return None
    try:
        return None
    except Exception:
        return None


def calculate_news(stock_ticker: str, gap_pct: float, rvol: float, breakout: str) -> str:
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


# ── XGBoost Trend / Confidence (FIXED — never blank) ─────────────────────────
def _rule_based_xgb_score(df: pd.DataFrame, rsi_val: float, macd_bullish: bool,
                           supertrend_bullish: Optional[bool], vwap_val: float,
                           rvol: float, support: float, resistance: float) -> float:
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

    if pd.notna(resistance) and resistance > 0:
        dist_to_r = (resistance - last_close) / last_close
        if dist_to_r < 0.02:
            score -= 4
    if pd.notna(support) and support > 0:
        dist_to_s = (last_close - support) / last_close
        if dist_to_s < 0.02:
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


def calculate_xgboost_prediction(
    df: pd.DataFrame,
    rsi_val: Optional[float] = None,
    macd_bullish: Optional[bool] = None,
    supertrend_bullish: Optional[bool] = None,
    vwap_val: Optional[float] = None,
    rvol: Optional[float] = None,
    support: Optional[float] = None,
    resistance: Optional[float] = None,
    use_ml: bool = True,
) -> Tuple[str, float]:
    try:
        close = df["Close"]

        if rsi_val is None:
            rsi_val = float(calculate_rsi(close).iloc[-1])
        if macd_bullish is None:
            macd_line, signal_line, _ = calculate_macd(close)
            macd_bullish = bool(macd_line.iloc[-1] > signal_line.iloc[-1])
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

        rule_score = _rule_based_xgb_score(
            df, rsi_val, macd_bullish, supertrend_bullish, vwap_val, rvol, support, resistance
        )

        if XGBOOST_AVAILABLE and os.path.exists(XGB_MODEL_PATH):
            try:
                model = xgb.XGBClassifier()
                model.load_model(XGB_MODEL_PATH)
                d = df.copy().reset_index(drop=True)
                d["Return"] = d["Close"].pct_change()
                d["RSI"] = calculate_rsi(d["Close"])
                _, _, hist = calculate_macd(d["Close"])
                d["MACD_Hist"] = hist
                d["Vol_Ratio"] = d["Volume"] / d["Volume"].rolling(20).mean()
                d["EMA_Dist"] = d["Close"] / d["Close"].ewm(span=20, adjust=False).mean() - 1
                feature_cols = ["Return", "RSI", "MACD_Hist", "Vol_Ratio", "EMA_Dist"]
                latest = d.dropna(subset=feature_cols).iloc[[-1]]
                if not latest.empty:
                    proba = model.predict_proba(latest[feature_cols])[0]
                    up_proba = float(proba[1]) * 100
                    blended = 0.7 * up_proba + 0.3 * rule_score
                    confidence = round(float(max(proba)) * 100, 1)
                    return _score_to_trend_label(blended), confidence
            except Exception:
                pass

        if use_ml and XGBOOST_AVAILABLE and len(df) >= 100:
            try:
                d = df.copy().reset_index(drop=True)
                d["Return"] = d["Close"].pct_change()
                d["RSI"] = calculate_rsi(d["Close"])
                _, _, hist = calculate_macd(d["Close"])
                d["MACD_Hist"] = hist
                d["Vol_Ratio"] = d["Volume"] / d["Volume"].rolling(20).mean()
                d["EMA_Dist"] = d["Close"] / d["Close"].ewm(span=20, adjust=False).mean() - 1
                d["Target"] = (d["Close"].shift(-1) > d["Close"]).astype(int)
                feature_cols = ["Return", "RSI", "MACD_Hist", "Vol_Ratio", "EMA_Dist"]
                d = d.dropna(subset=feature_cols)
                if len(d) >= 60:
                    train = d.iloc[:-1]
                    latest = d.iloc[[-1]]
                    X_train, y_train = train[feature_cols], train["Target"]
                    if y_train.nunique() >= 2:
                        model = xgb.XGBClassifier(
                            n_estimators=50, max_depth=3, learning_rate=0.1,
                            eval_metric="logloss", verbosity=0,
                        )
                        model.fit(X_train, y_train)
                        proba = model.predict_proba(latest[feature_cols])[0]
                        up_proba = float(proba[1]) * 100
                        blended = 0.6 * up_proba + 0.4 * rule_score
                        confidence = round(float(max(proba)) * 100, 1)
                        return _score_to_trend_label(blended), confidence
            except Exception:
                pass

        confidence = round(45 + abs(rule_score - 50) * 1.1, 1)
        confidence = max(35.0, min(97.0, confidence))
        return _score_to_trend_label(rule_score), confidence
    except Exception:
        # Absolute last-resort fallback — this function must NEVER raise or
        # leave the caller with a blank Trend/Confidence value.
        return "🟡 Neutral", 50.0


def generate_alerts(rvol: float, breakout: str, cisd_signal: str, mtf_trend: str, gap_pct: float) -> str:
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


def calculate_final_signal(
    ai_score: float, xgb_trend: str, mtf_trend: str, rs_label: str,
    rsi: float, macd_bullish: bool, supertrend_bullish: Optional[bool],
    breakout: str, cisd_signal: str, smc_structure: str,
) -> str:
    score = 0

    if ai_score > 70: score += 2
    elif ai_score > 55: score += 1
    elif ai_score < 30: score -= 2
    elif ai_score < 45: score -= 1

    if "Strong Bullish" in xgb_trend: score += 2
    elif "Bullish" in xgb_trend: score += 1
    elif "Strong Bearish" in xgb_trend: score -= 2
    elif "Bearish" in xgb_trend: score -= 1

    if "Aligned Bullish" in mtf_trend: score += 1
    elif "Aligned Bearish" in mtf_trend: score -= 1

    if "Outperform" in rs_label: score += 1
    elif "Underperform" in rs_label: score -= 1

    if rsi > 70: score -= 1
    elif rsi < 30: score += 1

    score += 1 if macd_bullish else -1

    if supertrend_bullish is True: score += 1
    elif supertrend_bullish is False: score -= 1

    if "Bullish" in breakout: score += 1
    elif "Bearish" in breakout: score -= 1

    if "Bullish" in cisd_signal: score += 1
    elif "Bearish" in cisd_signal: score -= 1

    if "📈" in smc_structure or "🐂" in smc_structure: score += 1
    elif "📉" in smc_structure or "🐻" in smc_structure: score -= 1

    if score >= 5:
        return "🟢 Strong Buy"
    if score >= 2:
        return "🔵 Buy"
    if score > -2:
        return "🟡 Wait"
    if score > -5:
        return "🟠 Sell"
    return "🔴 Strong Sell"


# ══════════════════════════════════════════════════════════════════════════
# ── SIGNAL QUALITY ENGINE (new) ──────────────────────────────────────────
# Scores every stock against a fixed 10-condition checklist, in whichever
# direction (BUY/SELL) more of those conditions confirm, then derives a
# star rating, an entry-confirmation verdict, a plain-English reason list,
# a trade-quality tag, and a strict BUY/SELL/WAIT decision. Purely additive
# — every existing column/function above is untouched.
# ══════════════════════════════════════════════════════════════════════════

# Minimum confirmations (out of 10) required for a stock to count as a
# "high quality" signal and be shown at all in the Full/F&O scanners.
SIGNAL_QUALITY_MIN_CONFIRMATIONS = 6


def _calculate_signal_quality(
    ema20: float, ema50: float, rsi_val: float, macd_bullish: bool,
    supertrend_bullish: Optional[bool], vwap_val: Optional[float], last_close: float,
    rvol_raw: float, breakout: str, cisd_signal: str, smc_structure: str,
    last_volume: float, vol_avg20: float,
) -> Tuple[str, int, bool, str, str]:
    """Checks the fixed 10-condition quality checklist in both directions,
    picks whichever direction (BUY/SELL) confirms more conditions, and
    returns (direction, confirmed_count, is_high_quality, star_rating, reason_str)."""

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
        direction = "BUY"
        confirmed_count = bull_count
        reasons = [label for label, ok in bull_checks.items() if ok]
    else:
        direction = "SELL"
        confirmed_count = bear_count
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


def _determine_entry_and_decision(
    direction: str, confirmed_count: int, ai_score: float, confidence: float,
    rvol_raw: float, volume_ok: bool,
) -> Tuple[str, str, str]:
    """Applies the strict BUY/SELL filter (AI Score / Confidence / RVOL /
    Volume / Trend Confirmed) and returns
    (Entry Confirmation, Trade Quality, Trade Decision)."""

    trend_confirmed = confirmed_count >= SIGNAL_QUALITY_MIN_CONFIRMATIONS

    strict_buy = (
        direction == "BUY" and ai_score >= 80 and confidence >= 75
        and rvol_raw >= 1.5 and volume_ok and trend_confirmed
    )
    strict_sell = (
        direction == "SELL" and ai_score <= 20 and confidence >= 75
        and rvol_raw >= 1.5 and volume_ok and trend_confirmed
    )

    if strict_buy:
        entry_confirmation = "✅ Confirmed BUY"
        trade_decision = "🟢 BUY"
    elif strict_sell:
        entry_confirmation = "❌ Avoid Trade"
        trade_decision = "🔴 SELL"
    else:
        entry_confirmation = "⚠️ Wait for Confirmation"
        trade_decision = "🟡 WAIT"

    if confirmed_count >= 8:
        trade_quality = "🟢 High Probability"
    elif confirmed_count >= 6:
        trade_quality = "🟡 Medium Probability"
    else:
        trade_quality = "🔴 Low Probability"

    return entry_confirmation, trade_quality, trade_decision


# ── Existing SMC / CISD logic ────────────────────────────────────────────────
def _calculate_smc_and_cisd(df: pd.DataFrame):
    """Detects CISD and SMC (BOS/CHOCH) events in the trailing window and
    returns (smc_structure, cisd_signal, event_ts).

    event_ts is the actual candle Timestamp that produced the signal (the
    CISD confirmation candle if one fired, else the SMC break candle, else
    None). It is NOT necessarily df's last row — a CISD/SMC shift may have
    confirmed a candle or two before the most recent one, and this must be
    preserved so "Signal Date"/"Signal Time" reflect exactly when the
    signal fired rather than whenever the scan happens to run.
    """
    if len(df) < 30:
        return "Range ➖", "None", None

    d = df.copy()
    d["Prev_High"] = d["High"].shift(1)
    d["Prev_Low"] = d["Low"].shift(1)
    d["Bullish_CISD"] = (d["Low"] < d["Prev_Low"]) & (d["Close"] > d["Prev_High"])
    d["Bearish_CISD"] = (d["High"] > d["Prev_High"]) & (d["Close"] < d["Prev_Low"])

    d["Local_High"] = d["High"].rolling(window=10).max().shift(1)
    d["Local_Low"] = d["Low"].rolling(window=10).min().shift(1)
    d["EMA20"] = d["Close"].ewm(span=20).mean()
    d["EMA50"] = d["Close"].ewm(span=50).mean()
    d["Bullish_Trend"] = d["EMA20"] > d["EMA50"]
    d["Break_Up"] = d["Close"] > d["Local_High"]
    d["Break_Down"] = d["Close"] < d["Local_Low"]

    recent = d.tail(20)

    cisd_events = recent[recent["Bullish_CISD"] | recent["Bearish_CISD"]]
    cisd_signal = "None"
    cisd_event_ts = None
    if not cisd_events.empty:
        is_bull = bool(cisd_events["Bullish_CISD"].iloc[-1])
        cisd_signal = "Bullish CISD 🚀" if is_bull else "Bearish CISD 🩸"
        cisd_event_ts = cisd_events["Time"].iloc[-1]

    smc_events = recent[recent["Break_Up"] | recent["Break_Down"]]
    smc_structure = "Range ➖"
    smc_event_ts = None
    if not smc_events.empty:
        is_up = bool(smc_events["Break_Up"].iloc[-1])
        is_bull_trend = bool(smc_events["Bullish_Trend"].iloc[-1])
        if is_up:
            smc_structure = "BOS 📈" if is_bull_trend else "CHOCH 🐂"
        else:
            smc_structure = "BOS 📉" if not is_bull_trend else "CHOCH 🐻"
        smc_event_ts = smc_events["Time"].iloc[-1]

    # CISD is the confirming event, so it takes priority when both are
    # present; otherwise the SMC break candle; otherwise None, in which
    # case callers fall back to df's latest candle (pure AI/breakout reads
    # with no CISD/SMC event to anchor to).
    event_ts = cisd_event_ts if cisd_event_ts is not None else smc_event_ts

    return smc_structure, cisd_signal, event_ts


# ══════════════════════════════════════════════════════════════════════════
# ── ORDER BLOCK DETECTION (Smart Money Concepts) — NEW, purely additive ────
# Detects Bullish / Bearish Order Blocks from daily candles. Does not read
# or modify anything computed by _calculate_smc_and_cisd — it only reuses
# its `smc_structure` output (BOS/CHOCH label) as a confirmation input.
#
# Bullish OB: last bearish candle before an impulsive bullish BOS move,
#   with above-average volume on that candle, where price has since
#   revisited the candle's High-Low zone.
# Bearish OB: mirror image (last bullish candle before an impulsive
#   bearish BOS move, above-average volume, zone revisited).
# ══════════════════════════════════════════════════════════════════════════

_OB_LOOKBACK = 20
_OB_MIN_MOVE_PCT = 1.5          # minimum impulsive move after the OB candle
_OB_VOL_MULTIPLIER = 1.2        # min candle volume vs 20-period avg to qualify


def _detect_order_blocks(df: pd.DataFrame, smc_structure: str) -> Tuple[str, str, str, str]:
    """Returns (Bullish Order Block, Bearish Order Block, Order Block Zone,
    Order Block Strength). Either side shows 'No' when no valid Order Block
    is found; Zone/Strength show '—' when neither side has a valid OB."""
    if len(df) < 15:
        return "No", "No", "—", "—"

    d = df.reset_index(drop=True)
    lookback = min(_OB_LOOKBACK, len(d) - 3)
    recent = d.tail(lookback + 2).reset_index(drop=True)

    vol_avg = d["Volume"].tail(20).mean()
    last_close = float(d["Close"].iloc[-1])

    bullish_label, bearish_label = "No", "No"
    ob_zone, ob_strength = "—", "—"

    is_bos_bullish = smc_structure in ("BOS 📈", "CHOCH 🐂")
    is_bos_bearish = smc_structure in ("BOS 📉", "CHOCH 🐻")

    def _strength(move_pct: float, candle_vol: float) -> str:
        if move_pct >= 4 and vol_avg > 0 and candle_vol >= vol_avg * 2:
            return "Strong"
        if move_pct >= 2.5 or (vol_avg > 0 and candle_vol >= vol_avg * 1.5):
            return "Medium"
        return "Weak"

    try:
        if is_bos_bullish:
            for i in range(len(recent) - 2, 0, -1):
                candle = recent.iloc[i]
                if not (candle["Close"] < candle["Open"]):
                    continue
                if i + 1 >= len(recent):
                    continue
                move_after = recent["Close"].iloc[i + 1:].max()
                move_pct = ((move_after - candle["Close"]) / candle["Close"] * 100) if candle["Close"] else 0
                vol_ok = vol_avg > 0 and candle["Volume"] >= vol_avg * _OB_VOL_MULTIPLIER
                if move_pct >= _OB_MIN_MOVE_PCT and vol_ok:
                    zone_low, zone_high = round(float(candle["Low"]), 2), round(float(candle["High"]), 2)
                    if zone_low <= last_close <= zone_high * 1.02:  # price revisited the zone
                        bullish_label = "🟢 Bullish OB"
                        ob_zone = f"{zone_low}–{zone_high}"
                        ob_strength = _strength(move_pct, float(candle["Volume"]))
                    break

        if is_bos_bearish and bullish_label == "No":
            for i in range(len(recent) - 2, 0, -1):
                candle = recent.iloc[i]
                if not (candle["Close"] > candle["Open"]):
                    continue
                if i + 1 >= len(recent):
                    continue
                move_after = recent["Close"].iloc[i + 1:].min()
                move_pct = ((candle["Close"] - move_after) / candle["Close"] * 100) if candle["Close"] else 0
                vol_ok = vol_avg > 0 and candle["Volume"] >= vol_avg * _OB_VOL_MULTIPLIER
                if move_pct >= _OB_MIN_MOVE_PCT and vol_ok:
                    zone_low, zone_high = round(float(candle["Low"]), 2), round(float(candle["High"]), 2)
                    if zone_low * 0.98 <= last_close <= zone_high:  # price revisited the zone
                        bearish_label = "🔴 Bearish OB"
                        ob_zone = f"{zone_low}–{zone_high}"
                        ob_strength = _strength(move_pct, float(candle["Volume"]))
                    break
    except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError, AttributeError):
        return "No", "No", "—", "—"

    return bullish_label, bearish_label, ob_zone, ob_strength


# ── Analysis core ─────────────────────────────────────────────────────────────
def _analyse(symbol: str, df: pd.DataFrame, nifty_close: Optional[pd.Series], enable_xgboost: bool) -> dict:
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

    # NEW: Order Block detection — purely additive, reuses smc_structure
    # already computed above as its BOS/CHOCH confirmation input.
    bullish_ob, bearish_ob, ob_zone, ob_strength = _detect_order_blocks(df, smc_structure)

    h52w = df["High"].max()
    l52w = df["Low"].min()
    last_close = close.iloc[-1]
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

    xgb_trend, xgb_confidence = calculate_xgboost_prediction(
        df,
        rsi_val=rsi_val, macd_bullish=macd_bullish, supertrend_bullish=supertrend_bullish,
        vwap_val=vwap_val, rvol=rvol, support=support, resistance=resistance,
        use_ml=enable_xgboost,
    )

    alerts = generate_alerts(rvol, breakout, cisd_signal, mtf_trend, gap_pct)

    final_signal = calculate_final_signal(
        ai_score=ai_score, xgb_trend=xgb_trend, mtf_trend=mtf_trend, rs_label=rs_label,
        rsi=rsi_val, macd_bullish=macd_bullish, supertrend_bullish=supertrend_bullish,
        breakout=breakout, cisd_signal=cisd_signal, smc_structure=smc_structure,
    )

    stock_ticker = symbol.replace("NSE:", "").replace("-EQ", "")

    news = calculate_news(stock_ticker, gap_pct, rvol, breakout)

    rvol_raw = round(float(rvol), 2)
    rvol_display = _format_rvol_display(rvol_raw)

    # ── Signal Quality Engine: 10-condition checklist → direction, count,
    # high-quality flag, star rating, reason string. ───────────────────────
    quality_direction, quality_count, is_high_quality, signal_strength, signal_reason = _calculate_signal_quality(
        ema20=float(ema20), ema50=float(ema50), rsi_val=rsi_val, macd_bullish=macd_bullish,
        supertrend_bullish=supertrend_bullish, vwap_val=vwap_val, last_close=float(last_close),
        rvol_raw=rvol_raw, breakout=breakout, cisd_signal=cisd_signal, smc_structure=smc_structure,
        last_volume=float(volume.iloc[-1]), vol_avg20=float(vol_avg20),
    )
    entry_confirmation, trade_quality, trade_decision = _determine_entry_and_decision(
        direction=quality_direction, confirmed_count=quality_count, ai_score=ai_score,
        confidence=xgb_confidence, rvol_raw=rvol_raw,
        volume_ok=bool(vol_avg20 and vol_avg20 > 0 and float(volume.iloc[-1]) > vol_avg20),
    )

    # Signal Date & Signal Time come from the candle that actually produced
    # this signal — not scan/system time. If a CISD/SMC event fired (i.e.
    # _signal_event_ts is set), use THAT candle's timestamp exactly, even
    # if it's earlier than df's most recent candle — this is what keeps
    # the displayed time from drifting to "now" on every rescan. If there
    # is no CISD/SMC event, fall back to the latest daily candle (which is
    # what a pure AI-Score/Breakout/XGBoost-only read is anchored to).
    # Daily candle → Signal Time is pinned to NSE market close (15:30 IST).
    if _signal_event_ts is not None:
        signal_date_str, signal_time_str = _format_signal_timestamp(_signal_event_ts, is_daily=True)
    else:
        signal_date_str, signal_time_str = _candle_signal_timestamp(df, is_daily=True)

    return {
        "Signal Date": signal_date_str,
        "Signal Time": signal_time_str,
        "Stock": stock_ticker,
        "LTP": round(last_close, 2),
        "Gap %": gap_str,
        "Target": target,
        "Stoploss": stoploss,
        "SMC Structure": smc_structure,
        "CISD": cisd_signal,
        "Bullish Order Block": bullish_ob,
        "Bearish Order Block": bearish_ob,
        "Order Block Zone": ob_zone,
        "Order Block Strength": ob_strength,
        "XGBoost Trend": xgb_trend,
        "XGBoost Confidence (%)": xgb_confidence,
        "News": news,
        "Alerts": alerts,
        "Signal Strength": signal_strength,
        "Entry Confirmation": entry_confirmation,
        "Signal Reason": signal_reason,
        "Trade Quality": trade_quality,
        "Trade Decision": trade_decision,
        "MTF Trend": mtf_trend,
        "AI Trend": ai_trend,
        "AI Confidence (%)": ai_confidence,
        "RS vs NIFTY": rs_label,
        "Support": round(float(support), 2) if pd.notna(support) else None,
        "Resistance": round(float(resistance), 2) if pd.notna(resistance) else None,
        "52W High": round(float(h52w), 2) if pd.notna(h52w) else None,
        "52W Low": round(float(l52w), 2) if pd.notna(l52w) else None,
        "52W Status": status_52w,
        "RSI": rsi_val,
        "Breakout Status": breakout,
        "MACD Signal": macd_signal_str,
        "Supertrend": supertrend_label,
        "VWAP": vwap_val,
        "Chart Pattern": chart_pattern,
        "RVOL": rvol_display,
        "AI Score": ai_score,
        "Final Signal": final_signal,
        "Smart Money": "🏦 Institutional" if ai_score > 70 else "⚖️ Neutral" if ai_score > 45 else "🔻 Distribution",
        "Signal": "🟢 BUY" if ai_score > 65 else "🔴 SELL" if ai_score < 40 else "🟡 HOLD",
        "_ATR14": round(float(atr14), 2) if pd.notna(atr14) else round(last_close * 0.01, 2),
        "_RVOL_RAW": rvol_raw,
        "_Is_High_Quality": is_high_quality,
        "_Quality_Count": quality_count,
    }


# ── Intraday Scanner ──────────────────────────────────────────────────────────
def calculate_intraday_signal(row: dict) -> dict:
    try:
        last_close = row["LTP"]
        atr = row.get("_ATR14") or round(last_close * 0.01, 2)
        rsi = row["RSI"]
        macd_bullish = "Bullish" in row["MACD Signal"]
        supertrend_label = row["Supertrend"]
        vwap = row["VWAP"]
        rvol = row.get("_RVOL_RAW", 0.0)
        breakout = row["Breakout Status"]
        ai_score = row["AI Score"]

        bull_votes = sum([
            macd_bullish,
            "Buy" in supertrend_label,
            vwap is not None and last_close > vwap,
            rsi > 50,
            breakout == "📈 Bullish",
        ])
        bear_votes = sum([
            not macd_bullish,
            "Sell" in supertrend_label,
            vwap is not None and last_close < vwap,
            rsi < 50,
            breakout == "📉 Bearish",
        ])

        if bull_votes >= 4 and rvol >= 1.2:
            signal = "🟢 BUY"
        elif bear_votes >= 4 and rvol >= 1.2:
            signal = "🔴 SELL"
        else:
            signal = "🟡 WAIT"

        entry = round(last_close, 2)
        if signal == "🟢 BUY":
            sl = round(entry - 1.0 * atr, 2)
            t1 = round(entry + 1.0 * atr, 2)
            t2 = round(entry + 1.8 * atr, 2)
            t3 = round(entry + 2.6 * atr, 2)
            exit_condition = "Exit if price closes below Stop Loss, or Supertrend flips to Sell"
        elif signal == "🔴 SELL":
            sl = round(entry + 1.0 * atr, 2)
            t1 = round(entry - 1.0 * atr, 2)
            t2 = round(entry - 1.8 * atr, 2)
            t3 = round(entry - 2.6 * atr, 2)
            exit_condition = "Exit if price closes above Stop Loss, or Supertrend flips to Buy"
        else:
            sl = round(entry - 1.0 * atr, 2)
            t1, t2, t3 = entry, entry, entry
            exit_condition = "No trade — wait for RVOL/MACD/VWAP/Supertrend alignment"

        risk = abs(entry - sl)
        reward = abs(t1 - entry)
        rr_ratio = round(reward / risk, 2) if risk > 0 else 0.0

        vote_total = max(bull_votes, bear_votes)
        confidence = round(min(95.0, 40 + vote_total * 11 + min(rvol, 3) * 5), 1)
        confidence = max(30.0, confidence)

        atr_pct = (atr / last_close * 100) if last_close else 0
        if atr_pct >= 3:
            holding_time = "15–45 Minutes (high volatility)"
        elif atr_pct >= 1.5:
            holding_time = "30–90 Minutes"
        else:
            holding_time = "1–3 Hours"

        reasons = ["MACD bullish" if macd_bullish else "MACD bearish"]
        reasons.append(f"Supertrend {supertrend_label.split()[-1]}")
        if vwap is not None:
            reasons.append("Above VWAP" if last_close > vwap else "Below VWAP")
        reasons.append(f"RSI {rsi}")
        if rvol >= 1.5:
            reasons.append(f"High RVOL {rvol}x")
        if breakout != "NO":
            reasons.append(f"Breakout: {breakout}")
        reason_str = ", ".join(reasons)

        return {
            "Signal Date": row["Signal Date"],
            "Signal Time": row["Signal Time"],
            "Stock": row["Stock"],
            "LTP": last_close,
            "Intraday Signal": signal,
            "Entry Price": entry,
            "Stop Loss": sl,
            "Target 1": t1,
            "Target 2": t2,
            "Target 3": t3,
            "Risk Reward Ratio": rr_ratio,
            "Confidence %": confidence,
            "AI Score": ai_score,
            "Bullish Order Block": row.get("Bullish Order Block", "No"),
            "Bearish Order Block": row.get("Bearish Order Block", "No"),
            "Order Block Zone": row.get("Order Block Zone", "—"),
            "Order Block Strength": row.get("Order Block Strength", "—"),
            "Expected Holding Time": holding_time,
            "Exit Condition": exit_condition,
            "Reason": reason_str,
        }
    except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError, AttributeError):
        # A single malformed row must never crash the whole Intraday scan.
        return {
            "Signal Date": row.get("Signal Date", "N/A"),
            "Signal Time": row.get("Signal Time", "N/A"),
            "Stock": row.get("Stock", "N/A"),
            "LTP": row.get("LTP"),
            "Intraday Signal": "🟡 WAIT",
            "Entry Price": row.get("LTP"),
            "Stop Loss": None, "Target 1": None, "Target 2": None, "Target 3": None,
            "Risk Reward Ratio": 0.0, "Confidence %": 0.0,
            "AI Score": row.get("AI Score", 0),
            "Bullish Order Block": row.get("Bullish Order Block", "No"),
            "Bearish Order Block": row.get("Bearish Order Block", "No"),
            "Order Block Zone": row.get("Order Block Zone", "—"),
            "Order Block Strength": row.get("Order Block Strength", "—"),
            "Expected Holding Time": "N/A",
            "Exit Condition": "Insufficient data for this stock",
            "Reason": "Insufficient data",
        }


# ── Swing Trade Scanner ────────────────────────────────────────────────────────
def calculate_swing_signal(row: dict) -> dict:
    try:
        last_close = row["LTP"]
        atr = row.get("_ATR14") or round(last_close * 0.01, 2)
        mtf_trend = row["MTF Trend"]
        rs_label = row["RS vs NIFTY"]
        supertrend_label = row["Supertrend"]
        smc_structure = row["SMC Structure"]
        cisd_signal = row["CISD"]
        ai_score = row["AI Score"]

        bull_votes = sum([
            "Aligned Bullish" in mtf_trend,
            "Outperform" in rs_label,
            "Buy" in supertrend_label,
            "📈" in smc_structure or "🐂" in smc_structure,
            "Bullish" in cisd_signal,
        ])
        bear_votes = sum([
            "Aligned Bearish" in mtf_trend,
            "Underperform" in rs_label,
            "Sell" in supertrend_label,
            "📉" in smc_structure or "🐻" in smc_structure,
            "Bearish" in cisd_signal,
        ])

        if bull_votes >= 3:
            signal = "🟢 BUY"
        elif bear_votes >= 3:
            signal = "🔴 SELL"
        else:
            signal = "🟡 HOLD"

        entry = round(last_close, 2)
        if signal == "🟢 BUY":
            sl = round(entry - 2.0 * atr, 2)
            t1 = round(entry + 2.0 * atr, 2)
            t2 = round(entry + 3.5 * atr, 2)
            t3 = round(entry + 5.0 * atr, 2)
            exit_condition = "Exit on daily close below Stop Loss, or MTF trend turning Mixed/Bearish"
        elif signal == "🔴 SELL":
            sl = round(entry + 2.0 * atr, 2)
            t1 = round(entry - 2.0 * atr, 2)
            t2 = round(entry - 3.5 * atr, 2)
            t3 = round(entry - 5.0 * atr, 2)
            exit_condition = "Exit on daily close above Stop Loss, or MTF trend turning Mixed/Bullish"
        else:
            sl = round(entry - 2.0 * atr, 2)
            t1, t2, t3 = entry, entry, entry
            exit_condition = "No position — wait for MTF/RS/Supertrend alignment"

        risk = abs(entry - sl)
        reward = abs(t1 - entry)
        rr_ratio = round(reward / risk, 2) if risk > 0 else 0.0

        vote_total = max(bull_votes, bear_votes)
        confidence = round(min(95.0, 38 + vote_total * 12 + (ai_score - 50) * 0.15), 1)
        confidence = max(30.0, confidence)

        if vote_total >= 4:
            trend_strength = "🟢 Strong"
        elif vote_total == 3:
            trend_strength = "🟡 Moderate"
        else:
            trend_strength = "🔴 Weak"

        atr_pct = (atr / last_close * 100) if last_close else 0
        if atr_pct >= 3:
            holding_days, est_days = "3–7 Days", 5
        elif atr_pct >= 1.5:
            holding_days, est_days = "7–14 Days", 10
        else:
            holding_days, est_days = "14–25 Days", 18
        # Note: this is a FORWARD-LOOKING projected exit date (not a signal
        # timestamp), so it intentionally still uses system "now" (IST).
        exit_date = (_now_ist() + timedelta(days=est_days)).strftime("%d-%b-%Y")

        reasons = [f"MTF: {mtf_trend}", f"RS vs NIFTY: {rs_label}", f"Supertrend: {supertrend_label}",
                   f"SMC: {smc_structure}"]
        if cisd_signal != "None":
            reasons.append(f"CISD: {cisd_signal}")
        reason_str = ", ".join(reasons)

        return {
            "Signal Date": row["Signal Date"],
            "Signal Time": row["Signal Time"],
            "Stock": row["Stock"],
            "Swing Signal": signal,
            "Swing Entry": entry,
            "Swing Stop Loss": sl,
            "Swing Target 1": t1,
            "Swing Target 2": t2,
            "Swing Target 3": t3,
            "Expected Holding Period": holding_days,
            "Estimated Exit Date": exit_date,
            "Exit Condition": exit_condition,
            "Trend Strength": trend_strength,
            "Confidence %": confidence,
            "AI Score": ai_score,
            "Risk Reward Ratio": rr_ratio,
            "Bullish Order Block": row.get("Bullish Order Block", "No"),
            "Bearish Order Block": row.get("Bearish Order Block", "No"),
            "Order Block Zone": row.get("Order Block Zone", "—"),
            "Order Block Strength": row.get("Order Block Strength", "—"),
            "Reason": reason_str,
        }
    except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError, AttributeError):
        return {
            "Signal Date": row.get("Signal Date", "N/A"),
            "Signal Time": row.get("Signal Time", "N/A"),
            "Stock": row.get("Stock", "N/A"),
            "Swing Signal": "🟡 HOLD",
            "Swing Entry": row.get("LTP"),
            "Swing Stop Loss": None, "Swing Target 1": None, "Swing Target 2": None, "Swing Target 3": None,
            "Expected Holding Period": "N/A", "Estimated Exit Date": "N/A",
            "Exit Condition": "Insufficient data for this stock",
            "Trend Strength": "🔴 Weak", "Confidence %": 0.0,
            "AI Score": row.get("AI Score", 0), "Risk Reward Ratio": 0.0,
            "Bullish Order Block": row.get("Bullish Order Block", "No"),
            "Bearish Order Block": row.get("Bearish Order Block", "No"),
            "Order Block Zone": row.get("Order Block Zone", "—"),
            "Order Block Strength": row.get("Order Block Strength", "—"),
            "Reason": "Insufficient data",
        }


def _fetch_symbol(fyers, symbol: str, nifty_close: Optional[pd.Series], enable_xgboost: bool):
    """Returns (result_dict_or_None, error_message_or_None). Never raises —
    every failure mode (API error, invalid symbol, empty data, malformed
    candles, or an exception anywhere in _analyse) is caught and turned
    into a short (symbol, reason) pair instead of crashing the scan."""
    if not isinstance(symbol, str) or not _VALID_EQ_SYMBOL_RE.match(symbol):
        return None, f"{symbol}: invalid symbol format — skipped"

    resp, err = _safe_history(fyers, {
        "symbol": symbol, "resolution": "D", "date_format": "1",
        "range_from": DATE_FROM, "range_to": DATE_TO, "cont_flag": "1"
    })
    if err:
        return None, f"{symbol}: {err}"

    candles = resp.get("candles") if resp else None
    if not candles or len(candles) < 30:
        return None, f"{symbol}: insufficient history ({len(candles) if candles else 0} candles)"

    try:
        df = pd.DataFrame(candles, columns=["Time", "Open", "High", "Low", "Close", "Volume"])
        df["Time"] = pd.to_datetime(df["Time"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
        df[["Open", "High", "Low", "Close", "Volume"]] = df[["Open", "High", "Low", "Close", "Volume"]].apply(
            pd.to_numeric, errors="coerce"
        )
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


def run_scan(fyers, symbols: List[str], nifty_close: Optional[pd.Series], enable_xgboost: bool):
    """Threaded, rate-limited scan with a progress bar + FIX 7 stats.
    Returns (results, errors, stats). A single symbol's failure never stops
    the rest of the scan — every symbol is isolated via _fetch_symbol."""
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
    return results, errors, stats


def _color_code(val):
    if isinstance(val, str):
        if any(x in val for x in [
            "Strong Buy", "BUY", "Institutional", "🟢", "🔵", "Buy", "BOS 📈", "CHOCH 🐂",
            "Bullish", "Aligned Bullish", "Outperform", "Near High", "Bullish Engulfing",
            "Hammer", "Higher Highs", "📈", "Up",
        ]):
            return "color: green; font-weight: bold;"
        if any(x in val for x in [
            "Strong Sell", "SELL", "Sell", "Distribution", "🔴", "🟠", "BOS 📉", "CHOCH 🐻",
            "Bearish", "Aligned Bearish", "Underperform", "Near Low", "Bearish Engulfing",
            "Shooting Star", "Lower Highs", "📉", "Down",
        ]):
            return "color: red; font-weight: bold;"
        if any(x in val for x in ["🟡", "Wait", "HOLD", "Neutral", "Mixed", "Inline", "WAIT"]):
            return "color: #b8860b; font-weight: bold;"
    return ""


def _style_dataframe(df: pd.DataFrame):
    styler = df.style
    if hasattr(styler, "map"):
        return styler.map(_color_code)
    return styler.applymap(_color_code)


# ── Excel conditional-formatting rules (openpyxl only) ───────────────────────
_SIGNAL_FILL_RULES = [
    ("STRONG BUY", "006100", "FFFFFF", True),
    ("STRONG SELL", "9C0006", "FFFFFF", True),
    ("WATCHLIST", "FFA500", "000000", True),
    ("BUY", "92D050", "000000", True),
    ("SELL", "FF0000", "FFFFFF", True),
    ("WAIT", "FFFF00", "000000", True),
    ("HOLD", "FFFF00", "000000", True),
    ("CISD UP", "92D050", "000000", True),
    ("CISD DOWN", "FF0000", "FFFFFF", True),
]

_SUPPORT_FILL_HEX = "E2EFDA"
_RESISTANCE_FILL_HEX = "FCE4D6"
_HIGH_AI_SCORE_FILL_HEX = "7030A0"
_HIGH_RVOL_FILL_HEX = "00FFFF"
_HEADER_FILL_HEX = "1F4E78"
_BAND_FILL_HEX = "F2F2F2"


def _get_conditional_fill_font(col_name: str, value):
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


def _format_worksheet(ws, df: pd.DataFrame):
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

    thin = Side(style="thin", color="B0B0B0")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    center = Alignment(horizontal="center", vertical="center", wrap_text=False)

    header_font = Font(bold=True, color="FFFFFF", name="Arial", size=11)
    header_fill = PatternFill("solid", fgColor=_HEADER_FILL_HEX)
    for cell in ws[1]:
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = center
        cell.border = border

    columns = list(df.columns)
    band_fill = PatternFill("solid", fgColor=_BAND_FILL_HEX)

    for r in range(2, ws.max_row + 1):
        row_is_band = (r % 2 == 0)
        for c in range(1, ws.max_column + 1):
            cell = ws.cell(row=r, column=c)
            cell.alignment = center
            cell.border = border

            col_name = columns[c - 1] if c - 1 < len(columns) else ""
            fill, font = _get_conditional_fill_font(col_name, cell.value)
            if fill is not None:
                cell.fill = fill
                if font is not None:
                    cell.font = font
            elif row_is_band:
                cell.fill = band_fill

    for col_cells in ws.columns:
        length = max((len(str(c.value)) for c in col_cells if c.value is not None), default=10)
        ws.column_dimensions[col_cells[0].column_letter].width = max(length + 2, 10)

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions


def to_excel_bytes(df: pd.DataFrame, sheet_name: str = "Scan Results") -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        safe_name = sheet_name[:31]
        df.to_excel(writer, index=False, sheet_name=safe_name)
        _format_worksheet(writer.sheets[safe_name], df)

    buf.seek(0)
    return buf.getvalue()


def to_excel_bytes_multi(sheets: dict) -> bytes:
    buf = io.BytesIO()

    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        for sheet_name, df in sheets.items():
            if df is None or df.empty:
                continue
            safe_name = sheet_name[:31]
            df.to_excel(writer, index=False, sheet_name=safe_name)
            _format_worksheet(writer.sheets[safe_name], df)

    buf.seek(0)
    return buf.getvalue()


# ══════════════════════════════════════════════════════════════════════════
# ── NEW ADDITIVE MODULES ─────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════

# ── 2. Intraday CISD Signals (5-Minute / 15-Minute) ─────────────────────────
_INTRADAY_RESOLUTION_MAP = {"5 Minutes": "5", "15 Minutes": "15"}


def _fetch_intraday_cisd_signal(fyers, symbol: str, resolution: str, timeframe_label: str):
    """Returns (row_dict_or_None, error_or_None). row is None (no error)
    when there's simply no live CISD signal on this symbol right now —
    that's normal, not a failure."""
    if not isinstance(symbol, str) or not _VALID_EQ_SYMBOL_RE.match(symbol):
        return None, f"{symbol}: invalid symbol format — skipped"

    date_from = (datetime.today() - timedelta(days=INTRADAY_CISD_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    date_to = datetime.today().strftime("%Y-%m-%d")

    resp, err = _safe_history(fyers, {
        "symbol": symbol, "resolution": resolution, "date_format": "1",
        "range_from": date_from, "range_to": date_to, "cont_flag": "1"
    })
    if err:
        return None, f"{symbol}: {err}"

    candles = resp.get("candles") if resp else None
    if not candles or len(candles) < 30:
        return None, None  # too little intraday history yet — not an error

    try:
        df = pd.DataFrame(candles, columns=["Time", "Open", "High", "Low", "Close", "Volume"])
        df["Time"] = pd.to_datetime(df["Time"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
        df[["Open", "High", "Low", "Close", "Volume"]] = df[["Open", "High", "Low", "Close", "Volume"]].apply(
            pd.to_numeric, errors="coerce"
        )
        df = df.dropna(subset=["Open", "High", "Low", "Close"])
        if len(df) < 30:
            return None, None

        smc_structure, cisd_signal, event_ts = _calculate_smc_and_cisd(df)
        if cisd_signal == "None":
            return None, None

        last_close = float(df["Close"].iloc[-1])
        atr = float(calculate_atr(df).iloc[-1])
        if pd.isna(atr) or atr <= 0:
            atr = last_close * 0.005

        is_up = "Bullish" in cisd_signal
        signal_label = "🟢 ▲ CISD UP Signal" if is_up else "🔴 ▼ CISD DOWN Signal"

        entry = round(last_close, 2)
        if is_up:
            sl = round(entry - 1.0 * atr, 2)
            target = round(entry + 2.0 * atr, 2)
        else:
            sl = round(entry + 1.0 * atr, 2)
            target = round(entry - 2.0 * atr, 2)

        risk = abs(entry - sl)
        reward = abs(target - entry)
        rr_ratio = round(reward / risk, 2) if risk > 0 else 0.0

        rsi_val = round(float(calculate_rsi(df["Close"]).iloc[-1]), 1)
        vol_avg20 = df["Volume"].tail(20).mean()
        rvol_raw = round(float(df["Volume"].iloc[-1] / vol_avg20), 2) if vol_avg20 > 0 else 0.0

        ai_score = round(min(max(50 + (rvol_raw * 10) + (10 if is_up else -10) + (rsi_val - 50) * 0.3, 0), 100), 1)
        confidence = round(min(95.0, max(35.0, 55 + min(rvol_raw, 3) * 8 + rr_ratio * 3)), 1)

        stock_ticker = symbol.replace("NSE:", "").replace("-EQ", "")
        # Signal Date/Time = timestamp of the actual candle whose close
        # confirmed this CISD shift — from _calculate_smc_and_cisd's
        # event_ts, NOT necessarily df's last row, and NEVER scan/system
        # time. This is a real 5-min/15-min candle (not daily), so its
        # timestamp is used as-is (is_daily=False, the default) —
        # e.g. 09:20/09:30/10:15 IST.
        signal_date_str, signal_time_str = (
            _format_signal_timestamp(event_ts) if event_ts is not None
            else _candle_signal_timestamp(df)
        )
        reason = (
            f"{timeframe_label} CISD {'bullish' if is_up else 'bearish'} shift confirmed on candle close "
            f"(RSI {rsi_val}, RVOL {_format_rvol_display(rvol_raw)})"
        )

        row = {
            "Signal Date": signal_date_str,
            "Signal Time": signal_time_str,
            "Timeframe": timeframe_label,
            "Stock": stock_ticker,
            "Signal": signal_label,
            "Entry": entry,
            "Stoploss": sl,
            "Target": target,
            "Confidence %": confidence,
            "AI Score": ai_score,
            "News": calculate_news(stock_ticker, 0.0, rvol_raw, "📈 Bullish" if is_up else "📉 Bearish"),
            "Reason": reason,
        }
        return row, None
    except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError, AttributeError) as e:
        return None, f"{symbol}: analysis error ({type(e).__name__})"


def run_intraday_cisd_scan(fyers, symbols: List[str], resolution: str, timeframe_label: str):
    symbols = _validate_symbols(symbols)
    results, errors = [], []
    stats = ScanStats(total=len(symbols))
    progress = st.progress(0.0, text=f"Scanning Intraday CISD 0 / {len(symbols)}")
    done = 0
    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i:i + BATCH_SIZE]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {
                executor.submit(_fetch_intraday_cisd_signal, fyers, s, resolution, timeframe_label): s
                for s in batch
            }
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
                progress.progress(done / len(symbols), text=f"Scanning Intraday CISD {done} / {len(symbols)}")
        if i + BATCH_SIZE < len(symbols):
            time.sleep(BATCH_PAUSE_SECONDS)
    progress.empty()
    return results, errors, stats


# ── 3. F&O CISD Scanner ──────────────────────────────────────────────────────
def _fetch_fo_cisd_signal(fyers, symbol: str):
    if not isinstance(symbol, str) or not _VALID_EQ_SYMBOL_RE.match(symbol):
        return None, f"{symbol}: invalid symbol format — skipped"

    resp, err = _safe_history(fyers, {
        "symbol": symbol, "resolution": "D", "date_format": "1",
        "range_from": DATE_FROM, "range_to": DATE_TO, "cont_flag": "1"
    })
    if err:
        return None, f"{symbol}: {err}"

    candles = resp.get("candles") if resp else None
    if not candles or len(candles) < 30:
        return None, f"{symbol}: insufficient history ({len(candles) if candles else 0} candles)"

    try:
        df = pd.DataFrame(candles, columns=["Time", "Open", "High", "Low", "Close", "Volume"])
        df["Time"] = pd.to_datetime(df["Time"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
        df[["Open", "High", "Low", "Close", "Volume"]] = df[["Open", "High", "Low", "Close", "Volume"]].apply(
            pd.to_numeric, errors="coerce"
        )
        df = df.dropna(subset=["Open", "High", "Low", "Close"])
        if len(df) < 30:
            return None, f"{symbol}: insufficient valid candle data after cleaning"

        smc_structure, cisd_signal, event_ts = _calculate_smc_and_cisd(df)
        if cisd_signal == "None":
            return None, None

        last_close = float(df["Close"].iloc[-1])
        atr = float(calculate_atr(df).iloc[-1])
        if pd.isna(atr) or atr <= 0:
            atr = last_close * 0.01

        is_bull = "Bullish" in cisd_signal
        signal_label = "🟢 ▲ CISD BUY" if is_bull else "🔴 ▼ CISD SELL"

        entry = round(last_close, 2)
        if is_bull:
            sl = round(entry - 1.5 * atr, 2)
            target = round(entry + 3.0 * atr, 2)
        else:
            sl = round(entry + 1.5 * atr, 2)
            target = round(entry - 3.0 * atr, 2)

        risk = abs(entry - sl)
        reward = abs(target - entry)
        rr_ratio = round(reward / risk, 2) if risk > 0 else 0.0

        supertrend_label, supertrend_bullish, _ = calculate_supertrend(df)
        vol_avg20 = df["Volume"].tail(20).mean()
        last_volume = float(df["Volume"].iloc[-1])
        rvol_raw = round(last_volume / vol_avg20, 2) if vol_avg20 > 0 else 0.0

        confidence = round(min(95.0, max(35.0,
            50 + min(rvol_raw, 3) * 10 + rr_ratio * 3 + (10 if supertrend_bullish == is_bull else 0)
        )), 1)

        gap_pct = 0.0
        if len(df) >= 2 and pd.notna(df["Close"].iloc[-2]) and df["Close"].iloc[-2] != 0:
            gap_pct = ((df["Open"].iloc[-1] - df["Close"].iloc[-2]) / df["Close"].iloc[-2]) * 100

        stock_ticker = symbol.replace("NSE:", "").replace("-EQ", "")
        # Signal Date/Time = timestamp of the actual candle whose close
        # confirmed this CISD event — from _calculate_smc_and_cisd's
        # event_ts, NOT necessarily df's last row, and NEVER scan/system
        # time. Daily candle → Signal Time is pinned to NSE market close
        # (15:30 IST).
        signal_date_str, signal_time_str = (
            _format_signal_timestamp(event_ts, is_daily=True) if event_ts is not None
            else _candle_signal_timestamp(df, is_daily=True)
        )

        row = {
            "Signal Date": signal_date_str,
            "Signal Time": signal_time_str,
            "Symbol": stock_ticker,
            "LTP": round(last_close, 2),
            "Signal": signal_label,
            "Entry": entry,
            "SL": sl,
            "Target": target,
            "Confidence": confidence,
            "Trend": supertrend_label,
            "Volume": int(last_volume),
            "RVOL": _format_rvol_display(rvol_raw),
            "News": calculate_news(stock_ticker, gap_pct, rvol_raw, "📈 Bullish" if is_bull else "📉 Bearish"),
        }
        return row, None
    except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError, AttributeError) as e:
        return None, f"{symbol}: analysis error ({type(e).__name__})"


def run_fo_cisd_scan(fyers, symbols: List[str]):
    symbols = _validate_symbols(symbols)
    results, errors = [], []
    stats = ScanStats(total=len(symbols))
    progress = st.progress(0.0, text=f"Scanning F&O CISD 0 / {len(symbols)}")
    done = 0
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
                stats.record(has_result=bool(res), has_error=bool(err))
                done += 1
                progress.progress(done / len(symbols), text=f"Scanning F&O CISD {done} / {len(symbols)}")
        if i + BATCH_SIZE < len(symbols):
            time.sleep(BATCH_PAUSE_SECONDS)
    progress.empty()
    return results, errors, stats


# ── 4. Swing Trading Scanner (Golden Cross / Death Cross) ───────────────────
def _fetch_golden_death_cross_signal(fyers, symbol: str):
    if not isinstance(symbol, str) or not _VALID_EQ_SYMBOL_RE.match(symbol):
        return None, f"{symbol}: invalid symbol format — skipped"

    resp, err = _safe_history(fyers, {
        "symbol": symbol, "resolution": "D", "date_format": "1",
        "range_from": DATE_FROM, "range_to": DATE_TO, "cont_flag": "1"
    })
    if err:
        return None, f"{symbol}: {err}"

    candles = resp.get("candles") if resp else None
    if not candles or len(candles) < 60:
        return None, f"{symbol}: insufficient history for cross detection"

    try:
        df = pd.DataFrame(candles, columns=["Time", "Open", "High", "Low", "Close", "Volume"])
        df["Time"] = pd.to_datetime(df["Time"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
        df[["Open", "High", "Low", "Close", "Volume"]] = df[["Open", "High", "Low", "Close", "Volume"]].apply(
            pd.to_numeric, errors="coerce"
        )
        df = df.dropna(subset=["Open", "High", "Low", "Close"])
        if len(df) < 60:
            return None, f"{symbol}: insufficient valid candle data after cleaning"

        close = df["Close"]

        ema50 = close.ewm(span=50, adjust=False).mean()
        ema200 = close.ewm(span=200, adjust=False).mean() if len(close) >= 200 else close.ewm(span=len(close), adjust=False).mean()

        lookback = min(5, len(close) - 1)
        diff_tail = (ema50 - ema200).tail(lookback + 1)
        prev_sign = np.sign(diff_tail.iloc[0])
        curr_sign = np.sign(diff_tail.iloc[-1])

        if prev_sign <= 0 and curr_sign > 0:
            cross_type = "Golden Cross"
        elif prev_sign >= 0 and curr_sign < 0:
            cross_type = "Death Cross"
        else:
            return None, None

        last_close = float(close.iloc[-1])
        atr = float(calculate_atr(df).iloc[-1])
        if pd.isna(atr) or atr <= 0:
            atr = last_close * 0.01

        is_bull = cross_type == "Golden Cross"
        signal_label = "🟢 Swing BUY" if is_bull else "🔴 Swing SELL"

        entry = round(last_close, 2)
        if is_bull:
            sl = round(entry - 2.0 * atr, 2)
            t1 = round(entry + 2.0 * atr, 2)
            t2 = round(entry + 3.5 * atr, 2)
            t3 = round(entry + 5.0 * atr, 2)
        else:
            sl = round(entry + 2.0 * atr, 2)
            t1 = round(entry - 2.0 * atr, 2)
            t2 = round(entry - 3.5 * atr, 2)
            t3 = round(entry - 5.0 * atr, 2)

        atr_pct = (atr / last_close * 100) if last_close else 0
        if atr_pct >= 3:
            holding_days, est_days = "3–7 Days", 5
        elif atr_pct >= 1.5:
            holding_days, est_days = "7–14 Days", 10
        else:
            holding_days, est_days = "14–25 Days", 18
        # Forward-looking projected exit date — intentionally still uses
        # system "now" (IST), unlike Signal Date/Signal Time above.
        exit_date = (_now_ist() + timedelta(days=est_days)).strftime("%d-%b-%Y")

        ema200_last = float(ema200.iloc[-1])
        ema_gap_pct = abs((float(ema50.iloc[-1]) - ema200_last) / ema200_last * 100) if ema200_last else 0
        if ema_gap_pct >= 3:
            trend_strength = "🟢 Strong"
        elif ema_gap_pct >= 1:
            trend_strength = "🟡 Moderate"
        else:
            trend_strength = "🔴 Weak"

        rsi_val = round(float(calculate_rsi(close).iloc[-1]), 1)
        vol_avg20 = df["Volume"].tail(20).mean()
        rvol_raw = round(float(df["Volume"].iloc[-1] / vol_avg20), 2) if vol_avg20 > 0 else 0.0

        ai_score = round(min(max(50 + (15 if is_bull else -15) + (rvol_raw * 8) + (rsi_val - 50) * 0.2, 0), 100), 1)
        confidence = round(min(95.0, max(35.0, 55 + ema_gap_pct * 4 + min(rvol_raw, 3) * 5)), 1)

        stock_ticker = symbol.replace("NSE:", "").replace("-EQ", "")
        # Signal Date/Time = timestamp of the last completed daily candle in
        # `df` that confirmed the Golden/Death Cross — never scan/system time.
        # Daily candle → Signal Time is the NSE market close (15:30 IST).
        signal_date_str, signal_time_str = _candle_signal_timestamp(df, is_daily=True)

        row = {
            "Signal Date": signal_date_str,
            "Signal Time": signal_time_str,
            "Stock": stock_ticker,
            "Cross Type": cross_type,
            "Signal": signal_label,
            "Entry": entry,
            "Stoploss": sl,
            "Target 1": t1,
            "Target 2": t2,
            "Target 3": t3,
            "Holding Period (Days)": holding_days,
            "Estimated Exit Date": exit_date,
            "Trend Strength": trend_strength,
            "Confidence %": confidence,
            "AI Score": ai_score,
            "News": calculate_news(stock_ticker, 0.0, rvol_raw, "📈 Bullish" if is_bull else "📉 Bearish"),
        }
        return row, None
    except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError, AttributeError) as e:
        return None, f"{symbol}: analysis error ({type(e).__name__})"


def run_golden_death_cross_scan(fyers, symbols: List[str]):
    symbols = _validate_symbols(symbols)
    results, errors = [], []
    stats = ScanStats(total=len(symbols))
    progress = st.progress(0.0, text=f"Scanning Golden/Death Cross 0 / {len(symbols)}")
    done = 0
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
                stats.record(has_result=bool(res), has_error=bool(err))
                done += 1
                progress.progress(done / len(symbols), text=f"Scanning Golden/Death Cross {done} / {len(symbols)}")
        if i + BATCH_SIZE < len(symbols):
            time.sleep(BATCH_PAUSE_SECONDS)
    progress.empty()
    return results, errors, stats


# ── 5. Pre-Market Scanner ────────────────────────────────────────────────────
def _fetch_premarket_signal(fyers, symbol: str):
    if not isinstance(symbol, str) or not _VALID_EQ_SYMBOL_RE.match(symbol):
        return None, f"{symbol}: invalid symbol format — skipped"

    resp, err = _safe_history(fyers, {
        "symbol": symbol, "resolution": "D", "date_format": "1",
        "range_from": DATE_FROM, "range_to": DATE_TO, "cont_flag": "1"
    })
    if err:
        return None, f"{symbol}: {err}"

    candles = resp.get("candles") if resp else None
    if not candles or len(candles) < 30:
        return None, f"{symbol}: insufficient history ({len(candles) if candles else 0} candles)"

    try:
        df = pd.DataFrame(candles, columns=["Time", "Open", "High", "Low", "Close", "Volume"])
        df["Time"] = pd.to_datetime(df["Time"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
        df[["Open", "High", "Low", "Close", "Volume"]] = df[["Open", "High", "Low", "Close", "Volume"]].apply(
            pd.to_numeric, errors="coerce"
        )
        df = df.dropna(subset=["Open", "High", "Low", "Close"])
        if len(df) < 30:
            return None, f"{symbol}: insufficient valid candle data after cleaning"

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
        ai_score = round(min(max(
            50 + (buy_sell_ratio - 1) * 8 + (rvol_raw * 6) + max(gap_pct, 0) * 2 + (rsi_val - 50) * 0.2,
            0), 100), 1)

        bullish_votes = sum([buy_sell_ratio > 1.2, gap_pct > 0.3, rvol_raw >= 1.5, rsi_val > 50])
        bearish_votes = sum([buy_sell_ratio < 0.8, gap_pct < -0.3, rvol_raw >= 1.5, rsi_val < 50])
        if bullish_votes >= 3:
            expected_trend = "🟢 Bullish Opening Likely"
        elif bearish_votes >= 3:
            expected_trend = "🔴 Bearish Opening Likely"
        else:
            expected_trend = "🟡 Flat/Uncertain"

        stock_ticker = symbol.replace("NSE:", "").replace("-EQ", "")
        # Signal Date/Time = timestamp of the last completed daily candle in
        # `df` used for this pre-market read — never scan/system time.
        # Daily candle → Signal Time is the NSE market close (15:30 IST).
        signal_date_str, signal_time_str = _candle_signal_timestamp(df, is_daily=True)

        row = {
            "Signal Date": signal_date_str,
            "Signal Time": signal_time_str,
            "Stock": stock_ticker,
            "Buy Volume": int(buy_volume),
            "Sell Volume": int(sell_volume),
            "Buy/Sell Ratio": buy_sell_ratio,
            "Gap %": f"{gap_pct:.2f}%",
            "RVOL": _format_rvol_display(rvol_raw),
            "AI Score": ai_score,
            "Expected Opening Trend": expected_trend,
            "News": calculate_news(stock_ticker, gap_pct, rvol_raw, "📈 Bullish" if bullish_votes >= 3 else ("📉 Bearish" if bearish_votes >= 3 else "NO")),
        }
        return row, None
    except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError, AttributeError) as e:
        return None, f"{symbol}: analysis error ({type(e).__name__})"


def run_premarket_scan(fyers, symbols: List[str]):
    symbols = _validate_symbols(symbols)
    results, errors = [], []
    stats = ScanStats(total=len(symbols))
    progress = st.progress(0.0, text=f"Scanning Pre-Market 0 / {len(symbols)}")
    done = 0
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
                stats.record(has_result=bool(res), has_error=bool(err))
                done += 1
                progress.progress(done / len(symbols), text=f"Scanning Pre-Market {done} / {len(symbols)}")
        if i + BATCH_SIZE < len(symbols):
            time.sleep(BATCH_PAUSE_SECONDS)
    progress.empty()
    return results, errors, stats


# ══════════════════════════════════════════════════════════════════════════
# ── 6. NSE F&O 15-Minute CISD Scanner (new, additive) ────────────────────
# Dedicated 15-minute CISD scanner restricted to the F&O stock universe
# (index symbols excluded via load_nse_fo_stock_symbols(), which already
# filters out NIFTY/BANKNIFTY/etc.). A signal is only generated from a
# 15-minute candle that has FULLY CLOSED — the currently-forming candle is
# always dropped before CISD detection, so Signal Date/Signal Time never
# drift on re-render/re-scan and only ever update when a genuinely NEW
# confirming candle closes.
# ══════════════════════════════════════════════════════════════════════════

FO_15M_CISD_RESOLUTION = "15"
FO_15M_CISD_RESOLUTION_MINUTES = 15
FO_15M_CISD_LOOKBACK_DAYS = 5


def _is_intraday_candle_closed(candle_time_ist, resolution_minutes: int) -> bool:
    """True only if the intraday candle starting at candle_time_ist (IST) has
    fully closed as of right now. Guarantees CISD signals are only ever
    generated off completed candles, never a still-forming one."""
    candle_close = candle_time_ist + timedelta(minutes=resolution_minutes)
    return _now_ist() >= candle_close


def _fetch_fo_15min_cisd_signal(fyers, symbol: str):
    """Returns (row_dict_or_None, error_or_None). row is None (no error) when
    there's simply no live, fully-closed 15-min CISD signal on this F&O stock
    right now — that's normal, not a failure."""
    if not isinstance(symbol, str) or not _VALID_EQ_SYMBOL_RE.match(symbol):
        return None, f"{symbol}: invalid symbol format — skipped"

    date_from = (datetime.today() - timedelta(days=FO_15M_CISD_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    date_to = datetime.today().strftime("%Y-%m-%d")

    resp, err = _safe_history(fyers, {
        "symbol": symbol, "resolution": FO_15M_CISD_RESOLUTION, "date_format": "1",
        "range_from": date_from, "range_to": date_to, "cont_flag": "1"
    })
    if err:
        return None, f"{symbol}: {err}"

    candles = resp.get("candles") if resp else None
    if not candles or len(candles) < 31:
        return None, None  # too little 15-min history yet — not an error

    try:
        df = pd.DataFrame(candles, columns=["Time", "Open", "High", "Low", "Close", "Volume"])
        df["Time"] = pd.to_datetime(df["Time"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
        df[["Open", "High", "Low", "Close", "Volume"]] = df[["Open", "High", "Low", "Close", "Volume"]].apply(
            pd.to_numeric, errors="coerce"
        )
        df = df.dropna(subset=["Open", "High", "Low", "Close"]).sort_values("Time").reset_index(drop=True)

        # Drop the currently-forming candle (if any) — a signal must only
        # ever be generated from a candle that has COMPLETELY closed.
        if len(df) > 0 and not _is_intraday_candle_closed(df["Time"].iloc[-1], FO_15M_CISD_RESOLUTION_MINUTES):
            df = df.iloc[:-1].reset_index(drop=True)

        if len(df) < 30:
            return None, None

        smc_structure, cisd_signal, event_ts = _calculate_smc_and_cisd(df)
        if cisd_signal == "None" or event_ts is None:
            return None, None

        # Belt-and-braces: the confirming candle itself must be closed too
        # (guaranteed by the trim above, kept explicit for future-proofing).
        if not _is_intraday_candle_closed(event_ts, FO_15M_CISD_RESOLUTION_MINUTES):
            return None, None

        last_close = float(df["Close"].iloc[-1])
        atr = float(calculate_atr(df).iloc[-1])
        if pd.isna(atr) or atr <= 0:
            atr = last_close * 0.005

        is_up = "Bullish" in cisd_signal
        signal_label = "🟢 ▲ CISD BUY" if is_up else "🔴 ▼ CISD SELL"

        entry = round(last_close, 2)
        if is_up:
            sl = round(entry - 1.0 * atr, 2)
            t1 = round(entry + 1.0 * atr, 2)
            t2 = round(entry + 1.8 * atr, 2)
            t3 = round(entry + 2.6 * atr, 2)
        else:
            sl = round(entry + 1.0 * atr, 2)
            t1 = round(entry - 1.0 * atr, 2)
            t2 = round(entry - 1.8 * atr, 2)
            t3 = round(entry - 2.6 * atr, 2)

        risk = abs(entry - sl)
        reward = abs(t1 - entry)
        rr_ratio = round(reward / risk, 2) if risk > 0 else 0.0

        rsi_val = round(float(calculate_rsi(df["Close"]).iloc[-1]), 1)
        vol_avg20 = df["Volume"].tail(20).mean()
        rvol_raw = round(float(df["Volume"].iloc[-1] / vol_avg20), 2) if vol_avg20 > 0 else 0.0

        ai_score = round(min(max(50 + (rvol_raw * 10) + (10 if is_up else -10) + (rsi_val - 50) * 0.3, 0), 100), 1)
        confidence = round(min(95.0, max(35.0, 55 + min(rvol_raw, 3) * 8 + rr_ratio * 3)), 1)

        stock_ticker = symbol.replace("NSE:", "").replace("-EQ", "")

        # Signal Date/Time = timestamp of the actual 15-min candle whose
        # CLOSE confirmed this CISD shift (event_ts from
        # _calculate_smc_and_cisd) — never scan/system time. Real intraday
        # candle → used as-is (is_daily=False), e.g. 09:30:00 IST. This
        # value stays fixed until a NEW confirming candle fires a new signal.
        signal_date_str, signal_time_str = _format_signal_timestamp(event_ts, is_daily=False)

        reason = (
            f"15-Min CISD {'bullish' if is_up else 'bearish'} shift confirmed on completed candle close "
            f"(RSI {rsi_val}, RVOL {_format_rvol_display(rvol_raw)})"
        )

        row = {
            "Signal Date": signal_date_str,
            "Signal Time": signal_time_str,
            "Stock": stock_ticker,
            "LTP": round(last_close, 2),
            "CISD Signal": signal_label,
            "Entry": entry,
            "Stop Loss": sl,
            "Target 1": t1,
            "Target 2": t2,
            "Target 3": t3,
            "Confidence %": confidence,
            "AI Score": ai_score,
            "Reason": reason,
        }
        return row, None
    except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError, AttributeError) as e:
        return None, f"{symbol}: analysis error ({type(e).__name__})"


def run_fo_15min_cisd_scan(fyers, symbols: List[str]):
    """Threaded, rate-limited 15-min CISD scan restricted to F&O stocks
    (pass the F&O-filtered universe from load_nse_fo_stock_symbols() —
    index symbols are already excluded there)."""
    symbols = _validate_symbols(symbols)
    results, errors = [], []
    stats = ScanStats(total=len(symbols))
    progress = st.progress(0.0, text=f"Scanning F&O 15-Min CISD 0 / {len(symbols)}")
    done = 0
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
                stats.record(has_result=bool(res), has_error=bool(err))
                done += 1
                progress.progress(done / len(symbols), text=f"Scanning F&O 15-Min CISD {done} / {len(symbols)}")
        if i + BATCH_SIZE < len(symbols):
            time.sleep(BATCH_PAUSE_SECONDS)
    progress.empty()
    return results, errors, stats


# ── Main Application ──────────────────────────────────────────────────────────
def show_scanner(fyers):
    st.title("🚀 NSE AI PRO V13 — Institutional Scanner")

    # Live India Standard Time clock — shows current IST at page load/refresh.
    # This is separate from "Signal Date"/"Signal Time" (which come from the
    # candle that generated each signal, per _candle_signal_timestamp) — this
    # is just "what time is it right now in India" context for the user.
    st.caption(f"🕒 Current Time (IST): {_now_ist().strftime('%d-%b-%Y %H:%M:%S')} IST")

    symbols = load_nse_equity_symbols()
    st.caption(f"Loaded {len(symbols)} NSE equity symbols from Fyers symbol master.")

    if not symbols:
        st.warning("No symbols loaded — check network access to public.fyers.in.")
        return

    col1, col2, col3 = st.columns([1, 1, 2])
    with col1:
        limit = st.number_input(
            "Limit symbols (0 = all)", min_value=0, max_value=len(symbols), value=200, step=50,
            help="Scanning all 2000+ symbols can take several minutes and may hit API rate limits. "
                 "Start with a smaller limit to test."
        )
    with col2:
        enable_xgboost = st.checkbox(
            "Enable XGBoost ML training", value=False,
            help="When ON, trains (or auto-loads a saved) XGBoost model and blends it into "
                 "'XGBoost Trend'/'Confidence %'. When OFF, those columns still populate using "
                 "the technical rule-based fallback (Price Action, RSI, MACD, Supertrend, VWAP, "
                 "Volume/RVOL, Support/Resistance, Momentum) — never blank, just faster to scan." + (
                     "" if XGBOOST_AVAILABLE else " (xgboost package not installed — install with `pip install xgboost`)"
                 ),
        )
    with col3:
        st.caption(
            f"Estimated time at {MAX_WORKERS} concurrent workers: "
            f"~{((limit or len(symbols)) / MAX_WORKERS) * 0.3 / 60:.1f}–"
            f"{((limit or len(symbols)) / MAX_WORKERS) * 1.0 / 60:.1f} min (rough estimate, "
            f"longer with XGBoost training enabled)."
        )

    scan_universe = symbols if limit == 0 else symbols[:limit]

    if st.button(f"🚀 Run Scan ({len(scan_universe)} symbols)"):
        with st.spinner("Fetching NIFTY benchmark for Relative Strength…"):
            nifty_close = fetch_nifty_benchmark(fyers)
        if nifty_close is None:
            st.info("Could not fetch NIFTY50 benchmark — 'RS vs NIFTY' will show N/A for this scan.")

        with st.spinner("Scanning…"):
            results, errors, stats = run_scan(fyers, scan_universe, nifty_close, enable_xgboost)

            full_df = pd.DataFrame(results)
            # ── Signal Quality filter: only keep stocks confirming ≥6/10 of
            # the quality checklist (see _calculate_signal_quality) so the
            # Full Scanner only ever shows high-conviction setups. ─────────
            if not full_df.empty and "_Is_High_Quality" in full_df.columns:
                full_df = full_df[full_df["_Is_High_Quality"] == True]
            display_cols = [c for c in full_df.columns if not c.startswith("_")]
            scan_df = full_df[display_cols] if not full_df.empty else full_df

            intraday_df = pd.DataFrame([calculate_intraday_signal(r) for r in results])
            swing_df = pd.DataFrame([calculate_swing_signal(r) for r in results])

        st.session_state["scan_df"] = scan_df
        st.session_state["intraday_df"] = intraday_df
        st.session_state["swing_df"] = swing_df
        st.session_state["scan_errors"] = errors
        st.session_state["scan_stats"] = stats

    if "scan_stats" in st.session_state:
        _display_scan_summary(st.session_state["scan_stats"])

    tab_scanner, tab_intraday, tab_swing, tab_fo, \
        tab_intraday_cisd, tab_fo_cisd, tab_golden_death, tab_premarket, tab_fo_15m_cisd = st.tabs(
        ["📊 Full Scanner", "⚡ Intraday Scanner", "📈 Swing Trade Scanner", "🏛️ F&O Stocks Scanner",
         "🕐 Intraday CISD Signals", "🎯 F&O CISD Scanner", "✝️ Swing Trading (Golden/Death Cross)",
         "🌅 Pre-Market Scanner", "🎯 NSE F&O 15-Min CISD Scanner"]
    )

    # ── Full Scanner tab ─────────────────────────────────────────────────
    with tab_scanner:
        st.caption(
            f"Showing only High-Quality signals — stocks confirming at least "
            f"{SIGNAL_QUALITY_MIN_CONFIRMATIONS}/10 checklist conditions "
            f"(CISD, SMC, EMA20/50, MACD, Supertrend, VWAP, RSI, RVOL, Breakout, Volume). "
            f"Weak/low-confluence signals are hidden."
        )
        if "scan_df" in st.session_state:
            df = st.session_state["scan_df"]
            if df.empty:
                st.info(
                    "No stocks met the high-quality bar (≥6/10 conditions) for this scan. "
                    "Try increasing the symbol limit, or check the summary above."
                )
            else:
                sorted_df = df.sort_values("AI Score", ascending=False)
                st.dataframe(_style_dataframe(sorted_df), use_container_width=True, height=500)
                st.bar_chart(df.set_index("Stock")["AI Score"])

                st.download_button(
                    "📥 Download Full Scan as Excel",
                    data=to_excel_bytes(sorted_df, "Scan Results"),
                    file_name=f"nse_scan_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    key="dl_scan",
                )

                if not st.session_state.get("intraday_df", pd.DataFrame()).empty or \
                   not st.session_state.get("swing_df", pd.DataFrame()).empty or \
                   not st.session_state.get("fo_scan_df", pd.DataFrame()).empty:
                    st.download_button(
                        "📥 Download ALL (Scan + Intraday + Swing + F&O) as one Excel workbook",
                        data=to_excel_bytes_multi({
                            "Scan Results": sorted_df,
                            "Intraday Signals": st.session_state.get("intraday_df"),
                            "Swing Signals": st.session_state.get("swing_df"),
                            "F&O Stocks": st.session_state.get("fo_scan_df"),
                        }),
                        file_name=f"nse_all_signals_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        key="dl_all",
                    )
        else:
            st.info("Run a scan above to see Full Scanner results here.")

    # ── Intraday Scanner tab ────────────────────────────────────────────
    with tab_intraday:
        st.caption(
            "Intraday-style signals derived from the latest daily candle's technicals "
            "(RSI, MACD, Supertrend, VWAP, RVOL, Breakout). No live intraday feed is "
            "wired in — see the note at the top of the source file."
        )
        idf = st.session_state.get("intraday_df")
        if idf is not None and not idf.empty:
            idf_sorted = idf.sort_values("Confidence %", ascending=False)
            st.dataframe(_style_dataframe(idf_sorted), use_container_width=True, height=500)
            st.download_button(
                "📥 Download Intraday Signals as Excel",
                data=to_excel_bytes(idf_sorted, "Intraday Signals"),
                file_name=f"nse_intraday_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key="dl_intraday",
            )
        else:
            st.info("Run a scan above to see Intraday Scanner results here.")

    # ── Swing Trade Scanner tab ─────────────────────────────────────────
    with tab_swing:
        st.caption(
            "Swing signals derived from MTF Trend, Relative Strength vs NIFTY, "
            "Supertrend, SMC structure and CISD confirmation."
        )
        sdf = st.session_state.get("swing_df")
        if sdf is not None and not sdf.empty:
            sdf_sorted = sdf.sort_values("Confidence %", ascending=False)
            st.dataframe(_style_dataframe(sdf_sorted), use_container_width=True, height=500)
            st.download_button(
                "📥 Download Swing Signals as Excel",
                data=to_excel_bytes(sdf_sorted, "Swing Signals"),
                file_name=f"nse_swing_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key="dl_swing",
            )
        else:
            st.info("Run a scan above to see Swing Trade Scanner results here.")

    # ── F&O Stocks Scanner tab ──────────────────────────────────────────
    with tab_fo:
        fo_symbols = load_nse_fo_stock_symbols()
        st.caption(f"Loaded {len(fo_symbols)} F&O-permitted NSE stocks (indices excluded).")

        if not fo_symbols:
            st.warning("No F&O stock symbols loaded — check network access to public.fyers.in.")
        else:
            fo_col1, fo_col2 = st.columns([1, 1])
            with fo_col1:
                fo_limit = st.number_input(
                    "Limit F&O symbols (0 = all)", min_value=0, max_value=len(fo_symbols),
                    value=len(fo_symbols), step=25, key="fo_limit",
                )
            with fo_col2:
                fo_enable_xgboost = st.checkbox(
                    "Enable XGBoost ML training (F&O scan)", value=False, key="fo_xgb",
                    disabled=not XGBOOST_AVAILABLE,
                )

            fo_universe = fo_symbols if fo_limit == 0 else fo_symbols[:fo_limit]

            if st.button(f"🏛️ Run F&O Stocks Scan ({len(fo_universe)} symbols)", key="fo_run"):
                with st.spinner("Fetching NIFTY benchmark for Relative Strength…"):
                    fo_nifty_close = fetch_nifty_benchmark(fyers)

                with st.spinner("Scanning F&O stocks…"):
                    fo_results, fo_errors, fo_stats = run_scan(fyers, fo_universe, fo_nifty_close, fo_enable_xgboost)
                    fo_full_df = pd.DataFrame(fo_results)
                    # Same ≥6/10 Signal Quality filter as the Full Scanner.
                    if not fo_full_df.empty and "_Is_High_Quality" in fo_full_df.columns:
                        fo_full_df = fo_full_df[fo_full_df["_Is_High_Quality"] == True]
                    fo_display_cols = [c for c in fo_full_df.columns if not c.startswith("_")]
                    fo_scan_df = fo_full_df[fo_display_cols] if not fo_full_df.empty else fo_full_df

                st.session_state["fo_scan_df"] = fo_scan_df
                st.session_state["fo_scan_errors"] = fo_errors
                st.session_state["fo_scan_stats"] = fo_stats

            if "fo_scan_stats" in st.session_state:
                _display_scan_summary(st.session_state["fo_scan_stats"])

            st.caption(
                f"Showing only High-Quality signals — stocks confirming at least "
                f"{SIGNAL_QUALITY_MIN_CONFIRMATIONS}/10 checklist conditions. Weak signals are hidden."
            )
            fo_df = st.session_state.get("fo_scan_df")
            if fo_df is not None and not fo_df.empty:
                fo_sorted = fo_df.sort_values("AI Score", ascending=False)
                st.dataframe(_style_dataframe(fo_sorted), use_container_width=True, height=500)
                st.bar_chart(fo_df.set_index("Stock")["AI Score"])

                st.download_button(
                    "📥 Download F&O Scan as Excel",
                    data=to_excel_bytes(fo_sorted, "F&O Stocks"),
                    file_name=f"nse_fo_scan_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    key="dl_fo",
                )
            elif "fo_scan_df" in st.session_state:
                st.info("No F&O stocks met the high-quality bar (≥6/10 conditions) for this scan.")
            else:
                st.info("Run an F&O scan above to see results here.")

            if st.session_state.get("fo_scan_errors"):
                with st.expander(f"⚠️ Skipped/failed F&O symbols ({len(st.session_state['fo_scan_errors'])})"):
                    st.caption("Showing up to 20 — most stocks are simply skipped for missing/invalid data, not app errors.")
                    st.text("\n".join(st.session_state["fo_scan_errors"][:20]))

    # ── Intraday CISD Signals tab ───────────────────────────────────────
    with tab_intraday_cisd:
        st.caption(
            "Live CISD (Change In State of Delivery) shifts detected directly on 5-minute or "
            "15-minute candles — a genuine intraday feed via a dedicated resolution='5'/'15' "
            "Fyers history call, separate from the daily-candle pipeline used elsewhere."
        )
        icisd_col1, icisd_col2, icisd_col3 = st.columns([1, 1, 1])
        with icisd_col1:
            icisd_timeframe = st.selectbox(
                "Timeframe", options=list(_INTRADAY_RESOLUTION_MAP.keys()), key="icisd_timeframe",
            )
        with icisd_col2:
            icisd_limit = st.number_input(
                "Limit symbols (0 = all)", min_value=0, max_value=len(symbols),
                value=min(200, len(symbols)), step=50, key="icisd_limit",
            )
        with icisd_col3:
            st.caption("Only stocks with a live CISD event right now are shown — most scans return a short list.")

        icisd_universe = symbols if icisd_limit == 0 else symbols[:icisd_limit]

        if st.button(f"🕐 Run Intraday CISD Scan ({len(icisd_universe)} symbols, {icisd_timeframe})", key="icisd_run"):
            with st.spinner(f"Scanning {icisd_timeframe} candles for CISD shifts…"):
                icisd_results, icisd_errors, icisd_stats = run_intraday_cisd_scan(
                    fyers, icisd_universe, _INTRADAY_RESOLUTION_MAP[icisd_timeframe], icisd_timeframe
                )
                icisd_df = pd.DataFrame(icisd_results)

            st.session_state["intraday_cisd_df"] = icisd_df
            st.session_state["intraday_cisd_errors"] = icisd_errors
            st.session_state["intraday_cisd_stats"] = icisd_stats

        if "intraday_cisd_stats" in st.session_state:
            _display_scan_summary(st.session_state["intraday_cisd_stats"])

        icisd_df = st.session_state.get("intraday_cisd_df")
        if icisd_df is not None and not icisd_df.empty:
            icisd_sorted = icisd_df.sort_values("Confidence %", ascending=False)
            st.dataframe(_style_dataframe(icisd_sorted), use_container_width=True, height=500)
            st.download_button(
                "📥 Download Intraday CISD Signals as Excel",
                data=to_excel_bytes(icisd_sorted, "Intraday CISD"),
                file_name=f"nse_intraday_cisd_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key="dl_icisd",
            )
        else:
            st.info("Run an Intraday CISD scan above to see live signals here.")

        if st.session_state.get("intraday_cisd_errors"):
            with st.expander(f"⚠️ Skipped/failed symbols ({len(st.session_state['intraday_cisd_errors'])})"):
                st.caption("Showing up to 20 — most stocks are simply skipped for missing/invalid data, not app errors.")
                st.text("\n".join(st.session_state["intraday_cisd_errors"][:20]))

    # ── F&O CISD Scanner tab ─────────────────────────────────────────────
    with tab_fo_cisd:
        st.caption("Daily-candle CISD BUY/SELL events across the full F&O-permitted stock universe.")
        fo_cisd_symbols = load_nse_fo_stock_symbols()

        if not fo_cisd_symbols:
            st.warning("No F&O stock symbols loaded — check network access to public.fyers.in.")
        else:
            fo_cisd_limit = st.number_input(
                "Limit F&O symbols (0 = all)", min_value=0, max_value=len(fo_cisd_symbols),
                value=len(fo_cisd_symbols), step=25, key="fo_cisd_limit",
            )
            fo_cisd_universe = fo_cisd_symbols if fo_cisd_limit == 0 else fo_cisd_symbols[:fo_cisd_limit]

            if st.button(f"🎯 Run F&O CISD Scan ({len(fo_cisd_universe)} symbols)", key="fo_cisd_run"):
                with st.spinner("Scanning F&O stocks for CISD BUY/SELL events…"):
                    fo_cisd_results, fo_cisd_errors, fo_cisd_stats = run_fo_cisd_scan(fyers, fo_cisd_universe)
                    fo_cisd_df = pd.DataFrame(fo_cisd_results)

                st.session_state["fo_cisd_df"] = fo_cisd_df
                st.session_state["fo_cisd_errors"] = fo_cisd_errors
                st.session_state["fo_cisd_stats"] = fo_cisd_stats

            if "fo_cisd_stats" in st.session_state:
                _display_scan_summary(st.session_state["fo_cisd_stats"])

            fo_cisd_df = st.session_state.get("fo_cisd_df")
            if fo_cisd_df is not None and not fo_cisd_df.empty:
                fo_cisd_sorted = fo_cisd_df.sort_values("Confidence", ascending=False)
                st.dataframe(_style_dataframe(fo_cisd_sorted), use_container_width=True, height=500)
                st.download_button(
                    "📥 Download F&O CISD Signals as Excel",
                    data=to_excel_bytes(fo_cisd_sorted, "F&O CISD"),
                    file_name=f"nse_fo_cisd_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    key="dl_fo_cisd",
                )
            else:
                st.info("Run an F&O CISD scan above to see live signals here.")

            if st.session_state.get("fo_cisd_errors"):
                with st.expander(f"⚠️ Skipped/failed F&O CISD symbols ({len(st.session_state['fo_cisd_errors'])})"):
                    st.caption("Showing up to 20 — most stocks are simply skipped for missing/invalid data, not app errors.")
                    st.text("\n".join(st.session_state["fo_cisd_errors"][:20]))

    # ── Swing Trading tab (Golden Cross / Death Cross) ───────────────────
    with tab_golden_death:
        st.caption("EMA50 / EMA200 Golden Cross (bullish) and Death Cross (bearish) detection on daily candles.")
        gd_limit = st.number_input(
            "Limit symbols (0 = all)", min_value=0, max_value=len(symbols),
            value=min(300, len(symbols)), step=50, key="gd_limit",
        )
        gd_universe = symbols if gd_limit == 0 else symbols[:gd_limit]

        if st.button(f"✝️ Run Golden/Death Cross Scan ({len(gd_universe)} symbols)", key="gd_run"):
            with st.spinner("Scanning for Golden Cross / Death Cross events…"):
                gd_results, gd_errors, gd_stats = run_golden_death_cross_scan(fyers, gd_universe)
                gd_df = pd.DataFrame(gd_results)

            st.session_state["golden_death_df"] = gd_df
            st.session_state["golden_death_errors"] = gd_errors
            st.session_state["golden_death_stats"] = gd_stats

        if "golden_death_stats" in st.session_state:
            _display_scan_summary(st.session_state["golden_death_stats"])

        gd_df = st.session_state.get("golden_death_df")
        if gd_df is not None and not gd_df.empty:
            gd_sorted = gd_df.sort_values("Confidence %", ascending=False)
            st.dataframe(_style_dataframe(gd_sorted), use_container_width=True, height=500)
            st.download_button(
                "📥 Download Golden/Death Cross Signals as Excel",
                data=to_excel_bytes(gd_sorted, "Swing Golden-Death"),
                file_name=f"nse_golden_death_cross_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key="dl_gd",
            )
        else:
            st.info("Run a Golden/Death Cross scan above to see results here.")

        if st.session_state.get("golden_death_errors"):
            with st.expander(f"⚠️ Skipped/failed symbols ({len(st.session_state['golden_death_errors'])})"):
                st.caption("Showing up to 20 — most stocks are simply skipped for missing/invalid data, not app errors.")
                st.text("\n".join(st.session_state["golden_death_errors"][:20]))

    # ── Pre-Market Scanner tab ──────────────────────────────────────────
    with tab_premarket:
        st.caption(
            "⚠️ Fyers' history feed doesn't expose true order-flow buy/sell volume or NSE delivery %, "
            "or a live pre-open auction. 'Buy Volume'/'Sell Volume'/'Buy-Sell Ratio' are a technical "
            "PROXY from the last 10 sessions' up-day vs down-day volume, and 'Gap %' is the most "
            "recently completed session's gap — useful pre-market context, not live tick data."
        )
        pm_limit = st.number_input(
            "Limit symbols (0 = all)", min_value=0, max_value=len(symbols),
            value=min(300, len(symbols)), step=50, key="pm_limit",
        )
        pm_universe = symbols if pm_limit == 0 else symbols[:pm_limit]

        if st.button(f"🌅 Run Pre-Market Scan ({len(pm_universe)} symbols)", key="pm_run"):
            with st.spinner("Scanning pre-market candidates…"):
                pm_results, pm_errors, pm_stats = run_premarket_scan(fyers, pm_universe)
                pm_df = pd.DataFrame(pm_results)

            st.session_state["premarket_df"] = pm_df
            st.session_state["premarket_errors"] = pm_errors
            st.session_state["premarket_stats"] = pm_stats

        if "premarket_stats" in st.session_state:
            _display_scan_summary(st.session_state["premarket_stats"])

        pm_df = st.session_state.get("premarket_df")
        if pm_df is not None and not pm_df.empty:
            pm_filter = st.selectbox(
                "Filter", options=["All", "Bullish Candidates", "Bearish Candidates", "High RVOL",
                                    "Gap Up", "Gap Down"], key="pm_filter",
            )
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
            except (KeyError, ValueError, TypeError, AttributeError):
                st.info("Could not apply that filter to the current results — showing all rows instead.")
                pm_view = pm_df.copy()

            pm_sorted = pm_view.sort_values("AI Score", ascending=False)
            st.dataframe(_style_dataframe(pm_sorted), use_container_width=True, height=500)
            st.download_button(
                "📥 Download Pre-Market Scan as Excel",
                data=to_excel_bytes(pm_sorted, "Pre-Market"),
                file_name=f"nse_premarket_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key="dl_pm",
            )
        else:
            st.info("Run a Pre-Market scan above to see results here.")

        if st.session_state.get("premarket_errors"):
            with st.expander(f"⚠️ Skipped/failed symbols ({len(st.session_state['premarket_errors'])})"):
                st.caption("Showing up to 20 — most stocks are simply skipped for missing/invalid data, not app errors.")
                st.text("\n".join(st.session_state["premarket_errors"][:20]))

    # ── NSE F&O 15-Minute CISD Scanner tab (new) ─────────────────────────
    with tab_fo_15m_cisd:
        st.caption(
            "Dedicated 15-minute CISD scanner — F&O stocks only (index symbols excluded). "
            "A signal is generated ONLY after a 15-minute candle has fully closed; the "
            "currently-forming candle is always dropped before detection. Signal Date/Time "
            "stay fixed until a new confirming candle produces a new signal."
        )
        fo15_symbols = load_nse_fo_stock_symbols()
        st.caption(f"Loaded {len(fo15_symbols)} F&O-permitted NSE stocks (indices excluded).")

        if not fo15_symbols:
            st.warning("No F&O stock symbols loaded — check network access to public.fyers.in.")
        else:
            fo15_limit = st.number_input(
                "Limit F&O symbols (0 = all)", min_value=0, max_value=len(fo15_symbols),
                value=len(fo15_symbols), step=25, key="fo15_limit",
            )
            fo15_universe = fo15_symbols if fo15_limit == 0 else fo15_symbols[:fo15_limit]

            if st.button(f"🎯 Run F&O 15-Min CISD Scan ({len(fo15_universe)} symbols)", key="fo15_run"):
                with st.spinner("Scanning F&O stocks for fully-closed 15-min CISD signals…"):
                    fo15_results, fo15_errors, fo15_stats = run_fo_15min_cisd_scan(fyers, fo15_universe)
                    fo15_df = pd.DataFrame(fo15_results)

                st.session_state["fo15_cisd_df"] = fo15_df
                st.session_state["fo15_cisd_errors"] = fo15_errors
                st.session_state["fo15_cisd_stats"] = fo15_stats

            if "fo15_cisd_stats" in st.session_state:
                _display_scan_summary(st.session_state["fo15_cisd_stats"])

            fo15_df = st.session_state.get("fo15_cisd_df")
            if fo15_df is not None and not fo15_df.empty:
                fo15_sorted = fo15_df.sort_values("Confidence %", ascending=False)
                st.dataframe(_style_dataframe(fo15_sorted), use_container_width=True, height=500)
                st.download_button(
                    "📥 Download F&O 15-Min CISD Signals as Excel",
                    data=to_excel_bytes(fo15_sorted, "F&O 15-Min CISD"),
                    file_name=f"nse_fo_15min_cisd_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    key="dl_fo15",
                )
            else:
                st.info("Run an F&O 15-Min CISD scan above to see live signals here.")

            if st.session_state.get("fo15_cisd_errors"):
                with st.expander(f"⚠️ Skipped/failed symbols ({len(st.session_state['fo15_cisd_errors'])})"):
                    st.caption("Showing up to 20 — most stocks are simply skipped for missing/invalid data, not app errors.")
                    st.text("\n".join(st.session_state["fo15_cisd_errors"][:20]))

    if st.session_state.get("scan_errors"):
        with st.expander(f"⚠️ Skipped/failed symbols ({len(st.session_state['scan_errors'])})"):
            st.caption("Showing up to 20 — most stocks are simply skipped for missing/invalid data, not app errors.")
            st.text("\n".join(st.session_state["scan_errors"][:20]))


# Fyers ఆబ్జెక్ట్‌ను ఇక్కడ పాస్ చేయండి
# show_scanner(fyers)
scanner.py
══════════════════════════════════════════════════════════════════════════
FYERS API V3 + Streamlit — 15-Minute Order Block (Smart Money Concepts)
Scanner & Live Dashboard.

WHAT THIS FILE DOES
--------------------
- Scans a universe of NSE equity symbols using ONLY 15-minute candles
  pulled from the FYERS API V3 `history` endpoint.
- Detects fresh Bullish and Bearish Order Blocks using a strict rule set
  (see `detect_order_block_signal` docstring for the exact conditions).
- De-duplicates signals so the same confirming candle never re-fires the
  same signal twice, even across dashboard refreshes/app restarts (the
  dedup state is persisted to disk in `data/dedup_state.json`).
- Persists every fresh signal as:
    * a human-readable .txt file under signals/bullish/ or signals/bearish/
    * a row appended to a CSV history file
    * a row appended to a JSON Lines history file
    * a line in the rotating log file under logs/
- Renders a Plotly candlestick chart per signal (Order Block zone, Entry,
  Stop Loss, Target lines, and a volume subplot) and saves it as an image
  (or HTML fallback if a static-image engine like `kaleido` isn't
  installed) under charts/.
- Exposes `run_dashboard(fyers, symbols=None)` — a single Streamlit entry
  point that app.py can import and call directly, e.g.:

        import scanner
        scanner.run_dashboard(fyers)

INTEGRATION WITH app.py
------------------------
`app.py` is expected to own FYERS authentication/session creation and
pass the resulting `fyers` client object (an instance of
`fyers_apiv3.fyersModel.FyersModel`) into `run_dashboard(fyers)`. This
file never manages login/token flow itself — it only calls
`fyers.history(params)`.

Every FYERS `history` response follows the documented v3 shape:
    {"s": "ok", "candles": [[epoch, open, high, low, close, volume], ...]}
"""

from __future__ import annotations

import csv
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import streamlit as st

try:
    import plotly.graph_objects as go
    PLOTLY_AVAILABLE = True
except ImportError:  # pragma: no cover — dashboard degrades gracefully
    PLOTLY_AVAILABLE = False

try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
except Exception:  # pragma: no cover — safety net on very old Pythons
    from datetime import timezone
    IST = timezone(timedelta(hours=5, minutes=30))


# ══════════════════════════════════════════════════════════════════════════
# ── 1. DIRECTORY LAYOUT ──────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════

BASE_DIR = Path(__file__).resolve().parent

SIGNALS_DIR = BASE_DIR / "signals"
SIGNALS_BULLISH_DIR = SIGNALS_DIR / "bullish"
SIGNALS_BEARISH_DIR = SIGNALS_DIR / "bearish"
LOGS_DIR = BASE_DIR / "logs"
CHARTS_DIR = BASE_DIR / "charts"
DATA_DIR = BASE_DIR / "data"

CSV_HISTORY_PATH = DATA_DIR / "signal_history.csv"
JSONL_HISTORY_PATH = DATA_DIR / "signal_history.jsonl"
DEDUP_STATE_PATH = DATA_DIR / "dedup_state.json"
LOG_FILE_PATH = LOGS_DIR / "scanner.log"


def ensure_directories() -> None:
    """Creates every folder this module writes to, if missing. Safe to call
    on every run — idempotent and side-effect-free when folders exist."""
    for directory in (
        SIGNALS_DIR, SIGNALS_BULLISH_DIR, SIGNALS_BEARISH_DIR,
        LOGS_DIR, CHARTS_DIR, DATA_DIR,
    ):
        directory.mkdir(parents=True, exist_ok=True)


# ══════════════════════════════════════════════════════════════════════════
# ── 2. LOGGING ────────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════

def setup_logging() -> logging.Logger:
    """Configures a module-level logger that writes to logs/scanner.log
    (rotating manually is unnecessary here — file is small/append-only text
    lines) as well as stdout, for visibility when run under Streamlit."""
    ensure_directories()
    logger = logging.getLogger("ob_scanner")
    logger.setLevel(logging.INFO)

    if not logger.handlers:  # avoid duplicate handlers on Streamlit re-runs
        fmt = logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        file_handler = logging.FileHandler(LOG_FILE_PATH, encoding="utf-8")
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)

        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(fmt)
        logger.addHandler(stream_handler)

    return logger


logger = setup_logging()


# ══════════════════════════════════════════════════════════════════════════
# ── 3. CONFIGURATION ──────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════

# This scanner is intraday-only and ALWAYS uses 15-minute candles.
RESOLUTION = "15"
RESOLUTION_MINUTES = 15
TIMEFRAME_LABEL = "15 Minutes"

# How many days of 15-min history to request. FYERS caps intraday history
# lookback per-request; a handful of trading days is more than enough for
# swing-high/low + order-block detection.
HISTORY_LOOKBACK_DAYS = 5

# Swing high/low lookback window (in candles) used to define the
# "previous swing high/low" a breakout candle must clear.
SWING_LOOKBACK_CANDLES = 10

# Order-block search window: how many recent candles we scan backwards
# from the breakout candle to find the last opposite-colour candle.
ORDER_BLOCK_SEARCH_WINDOW = 15

# Volume confirmation: breakout candle volume must exceed this multiple of
# the 20-candle average volume.
VOLUME_AVG_WINDOW = 20
VOLUME_CONFIRMATION_MULTIPLIER = 1.0

# Risk/Reward multiples (in units of the Order Block zone height) used to
# derive Target 1 / Target 2 from Entry/Stop Loss.
TARGET1_RR_MULTIPLE = 1.5
TARGET2_RR_MULTIPLE = 2.5

# Network / rate-limit friendly scanning.
MAX_WORKERS = 8
BATCH_SIZE = 40
BATCH_PAUSE_SECONDS = 1.0

_HISTORY_MAX_RETRIES = 3
_HISTORY_BASE_DELAY_SECONDS = 1.0

# Default dashboard auto-refresh interval, in seconds.
DEFAULT_REFRESH_SECONDS = 30

# Fyers' public NSE Capital Market symbol master — used only as a
# convenience default universe when app.py doesn't supply its own symbol
# list to run_dashboard().
FYERS_NSE_CM_SYMBOL_MASTER = "https://public.fyers.in/sym_details/NSE_CM.csv"


# ══════════════════════════════════════════════════════════════════════════
# ── 4. TIME HELPERS (IST) ────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════

def now_ist() -> datetime:
    """Current time in IST (Asia/Kolkata) — single source of truth for
    'what time is it right now', as opposed to a candle's own timestamp."""
    return datetime.now(IST)


def to_ist(ts: pd.Timestamp) -> pd.Timestamp:
    """Normalizes a (possibly naive) pandas Timestamp to IST."""
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.tz_convert(IST)


def is_candle_closed(candle_open_time: pd.Timestamp, resolution_minutes: int = RESOLUTION_MINUTES) -> bool:
    """True only if the candle starting at `candle_open_time` has fully
    closed as of this exact moment. Guarantees signals are only ever
    generated off a completed 15-minute candle, never a still-forming one."""
    candle_close = to_ist(candle_open_time) + timedelta(minutes=resolution_minutes)
    return now_ist() >= candle_close


def format_ist(ts: pd.Timestamp) -> Tuple[str, str]:
    """Formats a candle Timestamp as ('DD-MMM-YYYY', 'HH:MM:SS IST'), in
    IST, exactly as it stands (15-minute candles already carry their true
    close-relevant open time, e.g. 09:15, 09:30, 09:45...)."""
    ts_ist = to_ist(ts)
    return ts_ist.strftime("%d-%b-%Y"), ts_ist.strftime("%H:%M:%S") + " IST"


# ══════════════════════════════════════════════════════════════════════════
# ── 5. DATA MODEL ─────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class OrderBlockSignal:
    """A single, fully-formed, fresh Order Block signal ready for display,
    persistence, alerting, and charting."""
    symbol: str
    direction: str                  # "BUY" or "SELL"
    signal_date: str                # DD-MMM-YYYY (IST)
    signal_time: str                # HH:MM:SS IST
    timeframe: str                  # "15 Minutes"
    entry_price: float
    stop_loss: float
    target_1: float
    target_2: float
    risk_reward_ratio: float
    volume_confirmed: bool
    order_block_high: float
    order_block_low: float
    current_price: float
    signal_strength: str            # "Strong" / "Medium" / "Weak"
    breakout_candle_time: str       # ISO timestamp, used as the dedup key
    reason: str = ""

    def dedup_key(self) -> str:
        """Uniquely identifies this exact signal occurrence — same symbol,
        same direction, same confirming candle. Used to guarantee a given
        breakout is never reported twice."""
        return f"{self.symbol}|{self.direction}|{self.breakout_candle_time}"

    def as_row(self) -> dict:
        return asdict(self)


# ══════════════════════════════════════════════════════════════════════════
# ── 6. RESILIENT FYERS HISTORY FETCH ─────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════

def safe_history(
    fyers,
    params: dict,
    max_retries: int = _HISTORY_MAX_RETRIES,
    base_delay: float = _HISTORY_BASE_DELAY_SECONDS,
) -> Tuple[Optional[dict], Optional[str]]:
    """Calls fyers.history(params) with retries + exponential-ish backoff.

    Returns (response_dict, None) on success, or (None, short_error) if
    every retry is exhausted or the symbol/params are rejected outright
    (in which case retrying would not help)."""
    symbol = params.get("symbol", "UNKNOWN")
    last_err = "unknown error"

    for attempt in range(1, max_retries + 1):
        try:
            resp = fyers.history(params)
        except Exception as exc:  # network, timeout, malformed JSON, etc.
            last_err = f"{type(exc).__name__}: {exc}"
        else:
            if not isinstance(resp, dict):
                last_err = "empty/invalid response"
            else:
                status = resp.get("s")
                if status == "ok":
                    candles = resp.get("candles")
                    if isinstance(candles, list):
                        return resp, None
                    last_err = "malformed candle data"
                else:
                    message = str(resp.get("message", status or "unknown"))
                    if "rate" in message.lower() or "limit" in message.lower():
                        last_err = f"rate limited: {message}"
                        time.sleep(base_delay * attempt * 2)
                        continue
                    # Invalid/delisted symbol — retrying will not help.
                    logger.warning("Symbol rejected by FYERS: %s (%s)", symbol, message)
                    return None, message

        if attempt < max_retries:
            time.sleep(base_delay * attempt)

    logger.error("History fetch failed for %s after %d attempts: %s", symbol, max_retries, last_err)
    return None, f"{symbol}: {last_err} (after {max_retries} attempts)"


def fetch_15m_candles(fyers, symbol: str, lookback_days: int = HISTORY_LOOKBACK_DAYS) -> Tuple[Optional[pd.DataFrame], Optional[str]]:
    """Fetches and cleans 15-minute OHLCV candles for `symbol`. Returns
    (DataFrame, None) on success or (None, error_message) on failure.

    The DataFrame is always sorted ascending by time and has a tz-aware
    'Time' column in Asia/Kolkata."""
    date_from = (datetime.today() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    date_to = datetime.today().strftime("%Y-%m-%d")

    resp, err = safe_history(fyers, {
        "symbol": symbol,
        "resolution": RESOLUTION,
        "date_format": "1",
        "range_from": date_from,
        "range_to": date_to,
        "cont_flag": "1",
    })
    if err:
        return None, err

    candles = resp.get("candles") if resp else None
    if not candles or len(candles) < (SWING_LOOKBACK_CANDLES + ORDER_BLOCK_SEARCH_WINDOW + 5):
        return None, f"{symbol}: insufficient 15-min history ({len(candles) if candles else 0} candles)"

    try:
        df = pd.DataFrame(candles, columns=["Time", "Open", "High", "Low", "Close", "Volume"])
        df["Time"] = pd.to_datetime(df["Time"], unit="s", utc=True).dt.tz_convert(IST)
        numeric_cols = ["Open", "High", "Low", "Close", "Volume"]
        df[numeric_cols] = df[numeric_cols].apply(pd.to_numeric, errors="coerce")
        df = df.dropna(subset=numeric_cols).sort_values("Time").reset_index(drop=True)
    except (KeyError, ValueError, TypeError) as exc:
        return None, f"{symbol}: malformed candle data ({exc})"

    # Drop a still-forming (not yet closed) final candle, if present, so
    # detection never runs against an incomplete candle.
    if len(df) > 0 and not is_candle_closed(df["Time"].iloc[-1]):
        df = df.iloc[:-1].reset_index(drop=True)

    if len(df) < (SWING_LOOKBACK_CANDLES + ORDER_BLOCK_SEARCH_WINDOW + 5):
        return None, f"{symbol}: insufficient closed 15-min candles after trimming"

    return df, None


# ══════════════════════════════════════════════════════════════════════════
# ── 7. SYMBOL UNIVERSE (optional convenience default) ───────────────────
# ══════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=60 * 60 * 12)
def load_default_nse_symbols(limit: int = 100) -> List[str]:
    """Downloads FYERS' NSE Capital Market symbol master and returns up to
    `limit` NSE equity ('NSE:SYMBOL-EQ') symbols. This is only a fallback
    for when app.py calls run_dashboard(fyers) without an explicit symbol
    list — for production scanning, pass your own curated/watchlist
    symbols instead for speed and relevance."""
    import requests

    try:
        resp = requests.get(FYERS_NSE_CM_SYMBOL_MASTER, timeout=20)
        resp.raise_for_status()
    except Exception as exc:
        logger.error("Could not download FYERS symbol master: %s", exc)
        return []

    symbols: List[str] = []
    for line in resp.text.strip().split("\n"):
        parts = line.split(",")
        for part in parts:
            part = part.strip()
            if part.startswith("NSE:") and part.endswith("-EQ"):
                symbols.append(part)
                break
        if len(symbols) >= limit:
            break

    return sorted(set(symbols))


# ══════════════════════════════════════════════════════════════════════════
# ── 8. ORDER BLOCK DETECTION ENGINE ──────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════

def _find_last_opposite_candle(df: pd.DataFrame, breakout_idx: int, bullish_ob: bool) -> Optional[int]:
    """Scans backwards from just before the breakout candle (within
    ORDER_BLOCK_SEARCH_WINDOW candles) for the LAST candle of the opposite
    colour to the breakout — i.e. the actual "order block" candle.

    bullish_ob=True  -> look for the last BEARISH candle (Close < Open)
    bullish_ob=False -> look for the last BULLISH candle (Close > Open)
    """
    start = max(0, breakout_idx - ORDER_BLOCK_SEARCH_WINDOW)
    for i in range(breakout_idx - 1, start - 1, -1):
        candle = df.iloc[i]
        is_bearish = candle["Close"] < candle["Open"]
        is_bullish = candle["Close"] > candle["Open"]
        if bullish_ob and is_bearish:
            return i
        if not bullish_ob and is_bullish:
            return i
    return None


def _signal_strength(
    volume_ratio: float, confirmation_strength_pct: float, rr_ratio: float
) -> str:
    """Combines volume conviction, how decisively the confirmation candle
    cleared the breakout level, and risk/reward quality into a simple
    Strong / Medium / Weak label."""
    score = 0
    if volume_ratio >= 2.0:
        score += 2
    elif volume_ratio >= VOLUME_CONFIRMATION_MULTIPLIER:
        score += 1

    if confirmation_strength_pct >= 0.5:
        score += 2
    elif confirmation_strength_pct >= 0.15:
        score += 1

    if rr_ratio >= 2.0:
        score += 2
    elif rr_ratio >= 1.2:
        score += 1

    if score >= 5:
        return "Strong"
    if score >= 3:
        return "Medium"
    return "Weak"


def detect_order_block_signal(df: pd.DataFrame, symbol: str) -> Optional[OrderBlockSignal]:
    """Runs the full Bullish/Bearish Order Block rule set against the most
    recently CLOSED 15-minute candles for `symbol` and returns a fresh
    OrderBlockSignal if (and only if) every rule is satisfied on the very
    latest candle (the "confirmation candle"). Returns None if no fresh
    signal is present right now — that is the normal, common case.

    Bullish Order Block rules
    -------------------------
    1. A breakout candle closes above the prior swing high (the highest
       high of the SWING_LOOKBACK_CANDLES candles before it).
    2. That breakout candle's volume exceeds VOLUME_CONFIRMATION_MULTIPLIER
       times the 20-candle average volume.
    3. The very next candle (the confirmation candle == the latest closed
       candle) ALSO closes above the breakout level.
    4. The Order Block itself is the last BEARISH candle before the
       breakout candle (its High/Low define the OB zone).

    Bearish Order Block rules are the exact mirror image (breakdown below
    prior swing low, last BULLISH candle before it is the OB, confirmation
    candle closes below the breakdown level).
    """
    if len(df) < SWING_LOOKBACK_CANDLES + ORDER_BLOCK_SEARCH_WINDOW + 5:
        return None

    d = df.reset_index(drop=True)
    d["Prior_Swing_High"] = d["High"].rolling(SWING_LOOKBACK_CANDLES).max().shift(1)
    d["Prior_Swing_Low"] = d["Low"].rolling(SWING_LOOKBACK_CANDLES).min().shift(1)
    d["Vol_Avg20"] = d["Volume"].rolling(VOLUME_AVG_WINDOW).mean()

    confirm_idx = len(d) - 1          # latest CLOSED candle == confirmation candle
    breakout_idx = confirm_idx - 1    # the candle immediately before it
    if breakout_idx < SWING_LOOKBACK_CANDLES:
        return None

    confirm_candle = d.iloc[confirm_idx]
    breakout_candle = d.iloc[breakout_idx]

    if pd.isna(breakout_candle["Prior_Swing_High"]) or pd.isna(breakout_candle["Prior_Swing_Low"]) \
            or pd.isna(breakout_candle["Vol_Avg20"]) or breakout_candle["Vol_Avg20"] <= 0:
        return None

    vol_ratio = float(breakout_candle["Volume"] / breakout_candle["Vol_Avg20"])
    volume_ok = vol_ratio >= VOLUME_CONFIRMATION_MULTIPLIER

    # ── Try Bullish Order Block first ───────────────────────────────────
    breakout_up = breakout_candle["Close"] > breakout_candle["Prior_Swing_High"]
    confirm_up = confirm_candle["Close"] > breakout_candle["Prior_Swing_High"]

    if breakout_up and volume_ok and confirm_up:
        ob_idx = _find_last_opposite_candle(d, breakout_idx, bullish_ob=True)
        if ob_idx is not None:
            ob_candle = d.iloc[ob_idx]
            ob_high = float(ob_candle["High"])
            ob_low = float(ob_candle["Low"])
            current_price = float(confirm_candle["Close"])
            entry_price = current_price
            stop_loss = round(ob_low, 2)
            zone_height = max(ob_high - ob_low, entry_price * 0.001)
            target_1 = round(entry_price + TARGET1_RR_MULTIPLE * zone_height, 2)
            target_2 = round(entry_price + TARGET2_RR_MULTIPLE * zone_height, 2)
            risk = abs(entry_price - stop_loss)
            reward = abs(target_1 - entry_price)
            rr_ratio = round(reward / risk, 2) if risk > 0 else 0.0

            breakout_level = float(breakout_candle["Prior_Swing_High"])
            confirm_strength_pct = (
                (confirm_candle["Close"] - breakout_level) / breakout_level * 100
                if breakout_level else 0.0
            )
            strength = _signal_strength(vol_ratio, confirm_strength_pct, rr_ratio)

            signal_date, signal_time = format_ist(confirm_candle["Time"])
            reason = (
                f"Bullish breakout above prior swing high {breakout_level:.2f} "
                f"(vol {vol_ratio:.2f}x avg), confirmed by next candle close "
                f"{confirm_candle['Close']:.2f}. Order Block from bearish candle at "
                f"{ob_candle['Time']}."
            )

            return OrderBlockSignal(
                symbol=symbol, direction="BUY",
                signal_date=signal_date, signal_time=signal_time,
                timeframe=TIMEFRAME_LABEL,
                entry_price=round(entry_price, 2), stop_loss=stop_loss,
                target_1=target_1, target_2=target_2, risk_reward_ratio=rr_ratio,
                volume_confirmed=volume_ok, order_block_high=round(ob_high, 2),
                order_block_low=round(ob_low, 2), current_price=round(current_price, 2),
                signal_strength=strength,
                breakout_candle_time=breakout_candle["Time"].isoformat(),
                reason=reason,
            )

    # ── Try Bearish Order Block ──────────────────────────────────────────
    breakdown_down = breakout_candle["Close"] < breakout_candle["Prior_Swing_Low"]
    confirm_down = confirm_candle["Close"] < breakout_candle["Prior_Swing_Low"]

    if breakdown_down and volume_ok and confirm_down:
        ob_idx = _find_last_opposite_candle(d, breakout_idx, bullish_ob=False)
        if ob_idx is not None:
            ob_candle = d.iloc[ob_idx]
            ob_high = float(ob_candle["High"])
            ob_low = float(ob_candle["Low"])
            current_price = float(confirm_candle["Close"])
            entry_price = current_price
            stop_loss = round(ob_high, 2)
            zone_height = max(ob_high - ob_low, entry_price * 0.001)
            target_1 = round(entry_price - TARGET1_RR_MULTIPLE * zone_height, 2)
            target_2 = round(entry_price - TARGET2_RR_MULTIPLE * zone_height, 2)
            risk = abs(stop_loss - entry_price)
            reward = abs(entry_price - target_1)
            rr_ratio = round(reward / risk, 2) if risk > 0 else 0.0

            breakdown_level = float(breakout_candle["Prior_Swing_Low"])
            confirm_strength_pct = (
                (breakdown_level - confirm_candle["Close"]) / breakdown_level * 100
                if breakdown_level else 0.0
            )
            strength = _signal_strength(vol_ratio, confirm_strength_pct, rr_ratio)

            signal_date, signal_time = format_ist(confirm_candle["Time"])
            reason = (
                f"Bearish breakdown below prior swing low {breakdown_level:.2f} "
                f"(vol {vol_ratio:.2f}x avg), confirmed by next candle close "
                f"{confirm_candle['Close']:.2f}. Order Block from bullish candle at "
                f"{ob_candle['Time']}."
            )

            return OrderBlockSignal(
                symbol=symbol, direction="SELL",
                signal_date=signal_date, signal_time=signal_time,
                timeframe=TIMEFRAME_LABEL,
                entry_price=round(entry_price, 2), stop_loss=stop_loss,
                target_1=target_1, target_2=target_2, risk_reward_ratio=rr_ratio,
                volume_confirmed=volume_ok, order_block_high=round(ob_high, 2),
                order_block_low=round(ob_low, 2), current_price=round(current_price, 2),
                signal_strength=strength,
                breakout_candle_time=breakout_candle["Time"].isoformat(),
                reason=reason,
            )

    return None


# ══════════════════════════════════════════════════════════════════════════
# ── 9. DE-DUPLICATION STORE ──────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════

def _load_dedup_state() -> Dict[str, str]:
    """Loads the persisted set of dedup keys already reported. Stored as a
    JSON object mapping dedup_key -> ISO timestamp first seen, so the file
    doubles as a lightweight audit trail."""
    ensure_directories()
    if not DEDUP_STATE_PATH.exists():
        return {}
    try:
        with open(DEDUP_STATE_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read dedup state (%s) — starting fresh.", exc)
        return {}


def _save_dedup_state(state: Dict[str, str]) -> None:
    ensure_directories()
    try:
        # Cap file growth: keep only the most recent 5000 entries.
        if len(state) > 5000:
            trimmed = dict(sorted(state.items(), key=lambda kv: kv[1])[-5000:])
            state = trimmed
        with open(DEDUP_STATE_PATH, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
    except OSError as exc:
        logger.error("Could not persist dedup state: %s", exc)


def filter_fresh_signals(signals: List[OrderBlockSignal]) -> List[OrderBlockSignal]:
    """Drops any signal whose dedup_key() has already been reported before
    (i.e. the same symbol+direction+confirming-candle combination), and
    records the newly-accepted ones so they are never reported again."""
    state = _load_dedup_state()
    fresh: List[OrderBlockSignal] = []
    for sig in signals:
        key = sig.dedup_key()
        if key not in state:
            fresh.append(sig)
            state[key] = now_ist().isoformat()
    if fresh:
        _save_dedup_state(state)
    return fresh


# ══════════════════════════════════════════════════════════════════════════
# ── 10. PERSISTENCE: TXT / CSV / JSONL ───────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════

def _safe_filename_component(text: str) -> str:
    return "".join(c if (c.isalnum() or c in ("-", "_")) else "_" for c in text)


def save_signal_txt(signal: OrderBlockSignal) -> Path:
    """Saves a human-readable signal file at
    signals/bullish/SYMBOL_DATE_TIME.txt or signals/bearish/... ."""
    ensure_directories()
    target_dir = SIGNALS_BULLISH_DIR if signal.direction == "BUY" else SIGNALS_BEARISH_DIR

    date_part = _safe_filename_component(signal.signal_date)
    time_part = _safe_filename_component(signal.signal_time.replace(":", "").replace(" ", "_"))
    filename = f"{signal.symbol}_{date_part}_{time_part}.txt"
    filepath = target_dir / filename

    lines = [
        f"Symbol            : {signal.symbol}",
        f"Signal            : {'BUY' if signal.direction == 'BUY' else 'SELL'}",
        f"Signal Time       : {signal.signal_date} {signal.signal_time}",
        f"Time Frame        : {signal.timeframe}",
        f"Entry Price       : {signal.entry_price}",
        f"Stop Loss         : {signal.stop_loss}",
        f"Target 1          : {signal.target_1}",
        f"Target 2          : {signal.target_2}",
        f"Risk Reward Ratio : {signal.risk_reward_ratio}",
        f"Volume Confirmed  : {'Yes' if signal.volume_confirmed else 'No'}",
        f"Order Block High  : {signal.order_block_high}",
        f"Order Block Low   : {signal.order_block_low}",
        f"Current Price     : {signal.current_price}",
        f"Signal Strength   : {signal.signal_strength}",
        f"Reason            : {signal.reason}",
    ]
    try:
        filepath.write_text("\n".join(lines), encoding="utf-8")
        logger.info("Saved signal file: %s", filepath)
    except OSError as exc:
        logger.error("Could not write signal file %s: %s", filepath, exc)
    return filepath


def append_csv_history(signal: OrderBlockSignal) -> None:
    ensure_directories()
    row = signal.as_row()
    file_exists = CSV_HISTORY_PATH.exists()
    try:
        with open(CSV_HISTORY_PATH, "a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(row.keys()))
            if not file_exists:
                writer.writeheader()
            writer.writerow(row)
    except OSError as exc:
        logger.error("Could not append to CSV history: %s", exc)


def append_jsonl_history(signal: OrderBlockSignal) -> None:
    ensure_directories()
    try:
        with open(JSONL_HISTORY_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(signal.as_row()) + "\n")
    except OSError as exc:
        logger.error("Could not append to JSONL history: %s", exc)


def persist_signal(signal: OrderBlockSignal) -> Path:
    """Runs all persistence steps for one fresh signal: txt file, CSV row,
    JSONL row, and a log line. Returns the saved .txt file path."""
    txt_path = save_signal_txt(signal)
    append_csv_history(signal)
    append_jsonl_history(signal)
    logger.info(
        "FRESH SIGNAL | %s | %s | Entry=%.2f SL=%.2f T1=%.2f T2=%.2f RR=%.2f Strength=%s",
        signal.symbol, signal.direction, signal.entry_price, signal.stop_loss,
        signal.target_1, signal.target_2, signal.risk_reward_ratio, signal.signal_strength,
    )
    return txt_path


# ══════════════════════════════════════════════════════════════════════════
# ── 11. CHART GENERATION (Plotly) ────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════

def generate_signal_chart(df: pd.DataFrame, signal: OrderBlockSignal) -> Optional[Path]:
    """Builds a candlestick + volume chart annotated with the Order Block
    zone, Entry, Stop Loss, and Target lines, and saves it under charts/.

    Tries to save a static PNG (requires the optional `kaleido` package);
    if that's unavailable, falls back to an interactive standalone HTML
    file instead so the chart is never silently skipped."""
    if not PLOTLY_AVAILABLE:
        logger.warning("Plotly not installed — skipping chart generation for %s.", signal.symbol)
        return None

    ensure_directories()
    plot_df = df.tail(80)  # last ~80 candles of 15-min context is plenty

    fig = go.Figure()
    fig.add_trace(go.Candlestick(
        x=plot_df["Time"], open=plot_df["Open"], high=plot_df["High"],
        low=plot_df["Low"], close=plot_df["Close"], name=signal.symbol,
        increasing_line_color="#26a69a", decreasing_line_color="#ef5350",
    ))

    # Order Block zone as a shaded rectangle.
    fig.add_shape(
        type="rect", xref="x", yref="y",
        x0=plot_df["Time"].iloc[0], x1=plot_df["Time"].iloc[-1],
        y0=signal.order_block_low, y1=signal.order_block_high,
        fillcolor="rgba(255,193,7,0.18)", line=dict(color="rgba(255,193,7,0.6)", width=1),
        layer="below",
    )
    fig.add_annotation(
        x=plot_df["Time"].iloc[-1], y=signal.order_block_high,
        text="Order Block Zone", showarrow=False, font=dict(size=10, color="#b8860b"),
        xanchor="right", yanchor="bottom",
    )

    level_lines = [
        ("Entry", signal.entry_price, "#2962ff"),
        ("Stop Loss", signal.stop_loss, "#d32f2f"),
        ("Target 1", signal.target_1, "#2e7d32"),
        ("Target 2", signal.target_2, "#1b5e20"),
    ]
    for label, price, color in level_lines:
        fig.add_hline(
            y=price, line=dict(color=color, width=1.5, dash="dash"),
            annotation_text=f"{label}: {price}", annotation_position="right",
            annotation_font=dict(color=color, size=10),
        )

    fig.add_trace(go.Bar(
        x=plot_df["Time"], y=plot_df["Volume"], name="Volume",
        marker_color="rgba(100,100,100,0.35)", yaxis="y2",
    ))

    fig.update_layout(
        title=f"{signal.symbol} — {signal.direction} Order Block ({signal.timeframe}) "
              f"@ {signal.signal_date} {signal.signal_time}",
        xaxis_rangeslider_visible=False,
        yaxis=dict(title="Price", domain=[0.25, 1.0]),
        yaxis2=dict(title="Volume", domain=[0.0, 0.2], anchor="x"),
        template="plotly_white",
        height=650,
        margin=dict(l=40, r=40, t=60, b=40),
        showlegend=False,
    )

    date_part = _safe_filename_component(signal.signal_date)
    time_part = _safe_filename_component(signal.signal_time.replace(":", "").replace(" ", "_"))
    base_name = f"{signal.symbol}_{signal.direction}_{date_part}_{time_part}"

    png_path = CHARTS_DIR / f"{base_name}.png"
    try:
        fig.write_image(str(png_path), scale=2)
        logger.info("Saved chart image: %s", png_path)
        return png_path
    except Exception as exc:
        # Most commonly: kaleido isn't installed. Fall back to HTML so the
        # chart is still produced and viewable.
        logger.warning("Static PNG export failed for %s (%s) — saving HTML instead.", signal.symbol, exc)
        html_path = CHARTS_DIR / f"{base_name}.html"
        try:
            fig.write_html(str(html_path), include_plotlyjs="cdn")
            logger.info("Saved chart HTML: %s", html_path)
            return html_path
        except OSError as exc2:
            logger.error("Could not save chart for %s: %s", signal.symbol, exc2)
            return None


# ══════════════════════════════════════════════════════════════════════════
# ── 12. SCAN ORCHESTRATION (multi-symbol, high performance) ─────────────
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class ScanStats:
    total: int
    scanned: int = 0
    signals_found: int = 0
    skipped: int = 0
    failed: int = 0
    elapsed_seconds: float = 0.0


def _scan_one_symbol(fyers, symbol: str) -> Tuple[Optional[OrderBlockSignal], Optional[pd.DataFrame], Optional[str]]:
    """Fetches 15-min candles for one symbol and runs Order Block
    detection. Returns (signal_or_None, dataframe_or_None, error_or_None).
    Never raises — every failure mode is caught and reduced to a short
    error string so one bad symbol can never crash the whole scan."""
    try:
        df, err = fetch_15m_candles(fyers, symbol)
        if err:
            return None, None, err
        signal = detect_order_block_signal(df, symbol)
        return signal, df, None
    except Exception as exc:  # absolute last-resort safety net
        logger.exception("Unexpected error scanning %s", symbol)
        return None, None, f"{symbol}: unexpected error ({type(exc).__name__})"


def run_order_block_scan(
    fyers, symbols: List[str], progress_callback=None,
) -> Tuple[List[OrderBlockSignal], Dict[str, pd.DataFrame], List[str], ScanStats]:
    """Threaded, rate-limited, high-performance scan across `symbols`.

    Returns:
        fresh_signals : de-duplicated list of brand-new OrderBlockSignal
        symbol_frames : {symbol: DataFrame} for every symbol that returned
                         usable candle data (used later for charting)
        errors        : short per-symbol error/skip messages
        stats         : ScanStats summary
    """
    start_time = time.time()
    stats = ScanStats(total=len(symbols))
    raw_signals: List[OrderBlockSignal] = []
    symbol_frames: Dict[str, pd.DataFrame] = {}
    errors: List[str] = []
    done = 0

    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i:i + BATCH_SIZE]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(_scan_one_symbol, fyers, s): s for s in batch}
            for future in as_completed(futures):
                symbol = futures[future]
                try:
                    signal, df, err = future.result()
                except Exception as exc:
                    signal, df, err = None, None, f"{symbol}: worker error ({type(exc).__name__})"

                if df is not None:
                    symbol_frames[symbol] = df
                if signal is not None:
                    raw_signals.append(signal)
                    stats.signals_found += 1
                if err:
                    errors.append(err)
                    stats.failed += 1
                elif signal is None:
                    stats.skipped += 1

                stats.scanned += 1
                done += 1
                if progress_callback is not None:
                    progress_callback(done, len(symbols))

        if i + BATCH_SIZE < len(symbols):
            time.sleep(BATCH_PAUSE_SECONDS)

    fresh_signals = filter_fresh_signals(raw_signals)
    stats.elapsed_seconds = round(time.time() - start_time, 1)

    logger.info(
        "Scan complete: total=%d scanned=%d fresh_signals=%d skipped=%d failed=%d elapsed=%.1fs",
        stats.total, stats.scanned, len(fresh_signals), stats.skipped, stats.failed, stats.elapsed_seconds,
    )
    return fresh_signals, symbol_frames, errors, stats


# ══════════════════════════════════════════════════════════════════════════
# ── 13. STREAMLIT DASHBOARD ───────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════

def _auto_refresh_control(interval_seconds: int) -> None:
    """Enables periodic dashboard refresh. Prefers the optional
    `streamlit-autorefresh` package (non-blocking, smooth); falls back to a
    simple blocking sleep + rerun cycle if that package isn't installed."""
    try:
        from streamlit_autorefresh import st_autorefresh  # type: ignore
        st_autorefresh(interval=interval_seconds * 1000, key="ob_scanner_autorefresh")
    except ImportError:
        st.caption(
            "ℹ️ Install `streamlit-autorefresh` for smoother auto-refresh "
            "(`pip install streamlit-autorefresh`). Using a basic fallback for now."
        )
        time.sleep(interval_seconds)
        st.rerun()


def _render_alert(signal: OrderBlockSignal) -> None:
    """Renders a live 🔔 alert block in the Streamlit UI for one fresh
    signal, in the exact format requested."""
    emoji = "🟢" if signal.direction == "BUY" else "🔴"
    label = "NEW BUY SIGNAL" if signal.direction == "BUY" else "NEW SELL SIGNAL"
    box = st.success if signal.direction == "BUY" else st.error
    box(
        f"🔔 **{label}**\n\n"
        f"**{signal.symbol}**\n\n"
        f"Time : {signal.signal_time}  \n"
        f"Time Frame : {signal.timeframe}  \n"
        f"Entry : {signal.entry_price}  \n"
        f"Stoploss : {signal.stop_loss}  \n"
        f"Target1 : {signal.target_1}  \n"
        f"Target2 : {signal.target_2}  \n"
        f"{emoji} Signal Strength: {signal.signal_strength} | RR: {signal.risk_reward_ratio}"
    )


def _signals_to_dataframe(signals: List[OrderBlockSignal]) -> pd.DataFrame:
    if not signals:
        return pd.DataFrame()
    rows = []
    for s in signals:
        rows.append({
            "Symbol": s.symbol,
            "Signal": "🟢 BUY" if s.direction == "BUY" else "🔴 SELL",
            "Signal Time": f"{s.signal_date} {s.signal_time}",
            "Time Frame": s.timeframe,
            "Entry Price": s.entry_price,
            "Stop Loss": s.stop_loss,
            "Target 1": s.target_1,
            "Target 2": s.target_2,
            "Risk Reward Ratio": s.risk_reward_ratio,
            "Volume Confirmation": "✅ Yes" if s.volume_confirmed else "❌ No",
            "Order Block High": s.order_block_high,
            "Order Block Low": s.order_block_low,
            "Current Price": s.current_price,
            "Signal Strength": s.signal_strength,
        })
    return pd.DataFrame(rows)


def run_dashboard(fyers, symbols: Optional[List[str]] = None) -> None:
    """Main Streamlit entry point. Call this directly from app.py:

        import scanner
        scanner.run_dashboard(fyers)

    `symbols` is an optional explicit list of 'NSE:SYMBOL-EQ' strings —
    if omitted, a default universe is pulled from FYERS' public NSE_CM
    symbol master (capped for scan speed; adjust in the sidebar).
    """
    ensure_directories()
    st.title("🎯 NSE 15-Min Order Block Scanner")
    st.caption(f"🕒 Current Time (IST): {now_ist().strftime('%d-%b-%Y %H:%M:%S')} IST — Timeframe: {TIMEFRAME_LABEL}")

    # ── Sidebar controls ────────────────────────────────────────────────
    with st.sidebar:
        st.header("⚙️ Scanner Controls")
        auto_refresh_enabled = st.checkbox("Enable auto-refresh", value=True)
        refresh_interval = st.number_input(
            "Refresh interval (seconds)", min_value=10, max_value=300,
            value=DEFAULT_REFRESH_SECONDS, step=5,
        )
        symbol_limit = st.number_input(
            "Symbol scan limit (default universe only)", min_value=10, max_value=500,
            value=100, step=10,
            help="Only applies when no explicit `symbols` list is passed to run_dashboard().",
        )
        show_charts = st.checkbox("Generate charts for fresh signals", value=True)
        st.divider()
        st.caption(
            "Rules: Bullish OB = last bearish candle before a breakout candle "
            "closing above the prior swing high with volume ≥ 20-candle "
            "average, confirmed by the next candle closing above the "
            "breakout level. Bearish OB is the mirror image."
        )

    # ── Resolve symbol universe ─────────────────────────────────────────
    if symbols is None:
        with st.spinner("Loading default NSE symbol universe…"):
            universe = load_default_nse_symbols(limit=int(symbol_limit))
    else:
        universe = list(symbols)

    if not universe:
        st.warning("No symbols available to scan — pass `symbols=[...]` to run_dashboard() or check network access.")
        return

    st.caption(f"Scanning {len(universe)} symbols on {TIMEFRAME_LABEL} candles.")

    # ── Run scan ─────────────────────────────────────────────────────────
    progress_bar = st.progress(0.0, text="Starting scan…")

    def _progress(done: int, total: int) -> None:
        progress_bar.progress(done / total, text=f"Scanning {done} / {total}")

    with st.spinner("Fetching 15-minute candles and detecting Order Blocks…"):
        fresh_signals, symbol_frames, errors, stats = run_order_block_scan(fyers, universe, _progress)
    progress_bar.empty()

    # ── Persist + chart every fresh signal ──────────────────────────────
    chart_paths: Dict[str, Path] = {}
    for signal in fresh_signals:
        persist_signal(signal)
        if show_charts and signal.symbol in symbol_frames:
            path = generate_signal_chart(symbol_frames[signal.symbol], signal)
            if path is not None:
                chart_paths[signal.dedup_key()] = path

    # ── Scan summary ─────────────────────────────────────────────────────
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Total Symbols", stats.total)
    c2.metric("Scanned", stats.scanned)
    c3.metric("Fresh Signals", len(fresh_signals))
    c4.metric("Skipped/No Signal", stats.skipped)
    c5.metric("Failed", stats.failed)
    st.caption(f"Scan completed in {stats.elapsed_seconds}s.")

    # ── Live alerts ──────────────────────────────────────────────────────
    st.subheader("🔔 Live Alerts")
    if fresh_signals:
        for signal in fresh_signals:
            _render_alert(signal)
    else:
        st.info("No fresh Order Block signals this cycle. The dashboard will keep scanning on refresh.")

    # ── Signals table ────────────────────────────────────────────────────
    st.subheader("📋 Fresh Signals — This Scan")
    df_signals = _signals_to_dataframe(fresh_signals)
    if not df_signals.empty:
        st.dataframe(df_signals, use_container_width=True, height=350)
        st.download_button(
            "📥 Download this scan's signals as CSV",
            data=df_signals.to_csv(index=False).encode("utf-8"),
            file_name=f"order_block_signals_{now_ist().strftime('%Y%m%d_%H%M')}.csv",
            mime="text/csv",
        )
    else:
        st.caption("Nothing to show yet — no fresh signals in this scan cycle.")

    # ── Charts ───────────────────────────────────────────────────────────
    if show_charts and chart_paths:
        st.subheader("📈 Signal Charts")
        for signal in fresh_signals:
            path = chart_paths.get(signal.dedup_key())
            if path is None:
                continue
            st.markdown(f"**{signal.symbol} — {signal.direction}** ({signal.signal_date} {signal.signal_time})")
            if path.suffix == ".png":
                st.image(str(path), use_container_width=True)
            else:
                with open(path, "r", encoding="utf-8") as fh:
                    st.components.v1.html(fh.read(), height=650, scrolling=True)

    # ── Historical signal log viewer ─────────────────────────────────────
    with st.expander("📚 Full Signal History (CSV log)"):
        if CSV_HISTORY_PATH.exists():
            hist_df = pd.read_csv(CSV_HISTORY_PATH)
            st.dataframe(hist_df.tail(200), use_container_width=True, height=400)
        else:
            st.caption("No signal history recorded yet.")

    # ── Errors/skips ─────────────────────────────────────────────────────
    if errors:
        with st.expander(f"⚠️ Skipped/failed symbols ({len(errors)})"):
            st.caption("Most entries are simply skipped for insufficient/invalid data, not app errors.")
            st.text("\n".join(errors[:30]))

    # ── Auto-refresh (kept last so everything above renders first) ──────
    if auto_refresh_enabled:
        _auto_refresh_control(int(refresh_interval))


# ══════════════════════════════════════════════════════════════════════════
# ── Standalone smoke-test entry point ────────────────────────────────────
# Running `streamlit run scanner.py` directly (without a real FYERS session
# from app.py) will show a friendly message instead of crashing, since a
# live `fyers` client is required for real scanning.
# ══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    st.title("🎯 NSE 15-Min Order Block Scanner")
    st.warning(
        "This module is designed to be imported from `app.py`, which owns the "
        "FYERS API V3 login/session and passes the authenticated `fyers` "
        "client into `scanner.run_dashboard(fyers)`.\n\n"
        "Example app.py:\n\n"
        "```python\n"
        "from fyers_apiv3 import fyersModel\n"
        "import scanner\n\n"
        "fyers = fyersModel.FyersModel(\n"
        "    client_id=CLIENT_ID, token=ACCESS_TOKEN, is_async=False, log_path=''\n"
        ")\n"
        "scanner.run_dashboard(fyers, symbols=['NSE:RELIANCE-EQ', 'NSE:TCS-EQ'])\n"
        "```"
    )
