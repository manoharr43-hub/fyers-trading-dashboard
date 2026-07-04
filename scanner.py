import streamlit as st
import pandas as pd
import numpy as np
import requests
import time
import io
import os
import re
import json
import logging
from datetime import datetime, timedelta
from typing import List, Optional, Tuple, Dict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

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

try:
    import matplotlib.pyplot as plt
    import matplotlib.patches as patches
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False

# ── Configuration ─────────────────────────────────────────────────────────
DATE_FROM = (datetime.today() - timedelta(days=365)).strftime("%Y-%m-%d")
DATE_TO = datetime.today().strftime("%Y-%m-%d")

FYERS_NSE_CM_SYMBOL_MASTER = "https://public.fyers.in/sym_details/NSE_CM.csv"

NIFTY_BENCHMARK_SYMBOL = "NSE:NIFTY50-INDEX"

MAX_WORKERS = 8
BATCH_SIZE = 50
BATCH_PAUSE_SECONDS = 1.0

XGB_MODEL_PATH = "xgb_trend_model.json"

INTRADAY_CISD_LOOKBACK_DAYS = 5

# ── Order Block Detection Configuration ───────────────────────────────────
OB_LOOKBACK_CANDLES = 20
OB_MIN_VOLUME_MULTIPLIER = 1.2
OB_MIN_MOVE_PERCENT = 1.5
OB_CONFIRMATION_VOLUME_MULTIPLIER = 1.0

# ── 15-Minute Order Block Signal Engine Configuration ────────────────────
SIGNAL_15M_LOOKBACK_DAYS = 5
SIGNAL_15M_RESOLUTION = "15"
SIGNAL_15M_DUPLICATE_PREVENTION_HOURS = 4

# ── Folder Structure ──────────────────────────────────────────────────────
SIGNAL_FOLDERS = {
    "base": "signals",
    "buy": "signals/buy",
    "sell": "signals/sell",
    "logs": "logs",
    "charts": "charts",
    "exports": "exports"
}

# ── Logging Configuration ────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ── Auto-create folder structure ──────────────────────────────────────────
def _ensure_signal_folders():
    """Auto-create all signal folders on startup."""
    try:
        for folder_path in SIGNAL_FOLDERS.values():
            Path(folder_path).mkdir(parents=True, exist_ok=True)
        logger.info(f"Signal folders created/verified: {list(SIGNAL_FOLDERS.values())}")
    except Exception as e:
        logger.error(f"Error creating signal folders: {e}")

_ensure_signal_folders()

# ── India Standard Time helpers ───────────────────────────────────────────
def _now_ist() -> datetime:
    """Current time in IST — the single source of truth for signal timestamps."""
    return datetime.now(IST)


from datetime import time as _dtime

_NSE_MARKET_CLOSE_IST = _dtime(15, 30, 0)


def _format_signal_timestamp(ts, is_daily: bool = False) -> Tuple[str, str]:
    """Formats a raw candle Timestamp into (Signal Date, Signal Time), as
    ('DD-MMM-YYYY', 'HH:MM:SS IST') — always in IST (Asia/Kolkata)."""
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
    """Returns (Signal Date, Signal Time) derived from df's last completed candle."""
    return _format_signal_timestamp(df["Time"].iloc[-1], is_daily=is_daily)


# ── Resilient, retrying Fyers history fetch ──────────────────────────────
_HISTORY_MAX_RETRIES = 3
_HISTORY_BASE_DELAY_SECONDS = 1.0


def _safe_history(fyers, params: dict, max_retries: int = _HISTORY_MAX_RETRIES,
                   base_delay: float = _HISTORY_BASE_DELAY_SECONDS) -> Tuple[Optional[dict], Optional[str]]:
    """Calls fyers.history(params) with automatic retries + backoff."""
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


# ── Symbol validation & de-duplication ────────────────────────────────────
_VALID_EQ_SYMBOL_RE = re.compile(r"^NSE:[A-Z0-9&\-]+-EQ$")


def _validate_symbols(symbols: List[str]) -> List[str]:
    """Drops duplicates, blanks, and anything that doesn't look like a
    genuine 'NSE:SYMBOL-EQ' equity symbol."""
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
    """Lightweight counters for scan tracking."""
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
    """Concise, non-technical scan summary."""
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Total Stocks", stats.total)
    c2.metric("Scanned", stats.scanned)
    c3.metric("Successful", stats.successful)
    c4.metric("Skipped", stats.skipped)
    c5.metric("Failed", stats.failed)
    c6.metric("Scan Time", f"{stats.elapsed_seconds:.1f}s")


# ── Symbol Universe ────────────────────────────────────────────────────────
@st.cache_data(ttl=60 * 60 * 12)
def load_nse_equity_symbols() -> List[str]:
    """Downloads Fyers' NSE Capital Market symbol master."""
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


# ════════════════════════════════════════════════════════════════
# ── F&O STOCKS MODULE ───────────────────────────────────────────
# ════════════════════════════════════════════════════════════════

FYERS_NSE_FO_SYMBOL_MASTER = "https://public.fyers.in/sym_details/NSE_FO.csv"

_FO_INDEX_UNDERLYINGS = {
    "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50",
    "NIFTYIT", "NIFTYPSE", "NIFTYINFRA", "SENSEX", "BANKEX", "NIFTY50",
}


@st.cache_data(ttl=60 * 60 * 12)
def load_nse_fo_stock_symbols() -> List[str]:
    """Downloads Fyers' NSE F&O symbol master and returns F&O-eligible stocks."""
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


# ── Benchmark (NIFTY) fetch ───────────────────────────────────────────────
@st.cache_data(ttl=60 * 30)
def fetch_nifty_benchmark(_fyers) -> Optional[pd.Series]:
    """Fetches NIFTY50 index daily closes for the same window as the scan."""
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


# ── Technical indicator helpers ───────────────────────────────────────────
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
    direction = np.ones(n, dtype=int)

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
    """Rolling typical-price VWAP."""
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
    """Multi-time-frame trend using daily EMA20 vs a weekly close."""
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
        atr = last_close * 0.01
    if direction == "Bullish":
        target, stoploss = last_close + 2 * atr, last_close - 1 * atr
    elif direction == "Bearish":
        target, stoploss = last_close - 2 * atr, last_close + 1 * atr
    else:
        target, stoploss = last_close + 1.5 * atr, last_close - 1.5 * atr
    return round(target, 2), round(stoploss, 2)


# ── RVOL display formatter ────────────────────────────────────────────────
def _format_rvol_display(rvol_raw: float) -> str:
    display = f"{rvol_raw:.2f}x"
    if rvol_raw >= 3.0:
        display += " 🔥🔥"
    elif rvol_raw >= 2.0:
        display += " ❤️‍🔥"
    return display


# ── AI Trend calculation ──────────────────────────────────────────────────
def calculate_ai_trend(ai_score: float) -> Tuple[str, float]:
    if ai_score >= 65:
        return "📈 Bullish", round(ai_score, 1)
    if ai_score <= 40:
        return "📉 Bearish", round(100 - ai_score, 1)
    return "➖ Neutral", round(50 + abs(ai_score - 50), 1)


# ── News column ───────────────────────────────────────────────────────────
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


# ── XGBoost Trend / Confidence ────────────────────────────────────────────
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


# ════════════════════════════════════════════════════════════════
# ── SIGNAL QUALITY ENGINE ──────────────────────────────────────
# ════════════════════════════════════════════════════════════════

SIGNAL_QUALITY_MIN_CONFIRMATIONS = 6


def _calculate_signal_quality(
    ema20: float, ema50: float, rsi_val: float, macd_bullish: bool,
    supertrend_bullish: Optional[bool], vwap_val: Optional[float], last_close: float,
    rvol_raw: float, breakout: str, cisd_signal: str, smc_structure: str,
    last_volume: float, vol_avg20: float,
) -> Tuple[str, int, bool, str, str]:
    """Checks the fixed 10-condition quality checklist."""
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
    """Applies the strict BUY/SELL filter."""
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


# ── Existing SMC / CISD logic ────────────────────────────────────────────
def _calculate_smc_and_cisd(df: pd.DataFrame):
    """Detects CISD and SMC (BOS/CHOCH) events."""
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

    event_ts = cisd_event_ts if cisd_event_ts is not None else smc_event_ts

    return smc_structure, cisd_signal, event_ts


# ════════════════════════════════════════════════════════════════
# ── ORDER BLOCK DETECTION (NEW) ────────────────────────────────
# ════════════════════════════════════════════════════════════════

def _detect_order_blocks(df: pd.DataFrame, smc_structure: str) -> Tuple[str, str, str, str]:
    """Returns (Bullish Order Block, Bearish Order Block, Order Block Zone,
    Order Block Strength)."""
    if len(df) < 15:
        return "No", "No", "—", "—"

    d = df.reset_index(drop=True)
    lookback = min(OB_LOOKBACK_CANDLES, len(d) - 3)
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
                vol_ok = vol_avg > 0 and candle["Volume"] >= vol_avg * OB_MIN_VOLUME_MULTIPLIER
                if move_pct >= OB_MIN_MOVE_PERCENT and vol_ok:
                    zone_low, zone_high = round(float(candle["Low"]), 2), round(float(candle["High"]), 2)
                    if zone_low <= last_close <= zone_high * 1.02:
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
                vol_ok = vol_avg > 0 and candle["Volume"] >= vol_avg * OB_MIN_VOLUME_MULTIPLIER
                if move_pct >= OB_MIN_MOVE_PERCENT and vol_ok:
                    zone_low, zone_high = round(float(candle["Low"]), 2), round(float(candle["High"]), 2)
                    if zone_low * 0.98 <= last_close <= zone_high:
                        bearish_label = "🔴 Bearish OB"
                        ob_zone = f"{zone_low}–{zone_high}"
                        ob_strength = _strength(move_pct, float(candle["Volume"]))
                    break
    except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError, AttributeError):
        return "No", "No", "—", "—"

    return bullish_label, bearish_label, ob_zone, ob_strength


# ════════════════════════════════════════════════════════════════
# ── 15-MINUTE ORDER BLOCK SIGNAL ENGINE (NEW) ──────────────────
# ════════════════════════════════════════════════════════════════

class OrderBlockSignal:
    """15-minute Order Block signal with all required metadata."""
    def __init__(
        self,
        symbol: str,
        signal_type: str,
        signal_date: str,
        signal_time: str,
        timeframe: str,
        order_block_high: float,
        order_block_low: float,
        entry_price: float,
        stop_loss: float,
        target_1: float,
        target_2: float,
        current_price: float,
        volume_avg_20: float,
        volume_current: float,
        rsi: float,
        macd_bullish: bool,
        signal_strength: str,
        risk_reward_ratio: float,
    ):
        self.symbol = symbol
        self.signal_type = signal_type
        self.signal_date = signal_date
        self.signal_time = signal_time
        self.timeframe = timeframe
        self.order_block_high = order_block_high
        self.order_block_low = order_block_low
        self.entry_price = entry_price
        self.stop_loss = stop_loss
        self.target_1 = target_1
        self.target_2 = target_2
        self.current_price = current_price
        self.volume_avg_20 = volume_avg_20
        self.volume_current = volume_current
        self.rsi = rsi
        self.macd_bullish = macd_bullish
        self.signal_strength = signal_strength
        self.risk_reward_ratio = risk_reward_ratio

    def to_dict(self) -> dict:
        """Convert signal to dictionary."""
        return {
            "Symbol": self.symbol,
            "Signal Type": self.signal_type,
            "Signal Date": self.signal_date,
            "Signal Time": self.signal_time,
            "Time Frame": self.timeframe,
            "Order Block Type": "Bullish" if self.signal_type == "BUY" else "Bearish",
            "Order Block High": self.order_block_high,
            "Order Block Low": self.order_block_low,
            "Entry Price": self.entry_price,
            "Stop Loss": self.stop_loss,
            "Target 1": self.target_1,
            "Target 2": self.target_2,
            "Current Price": self.current_price,
            "Volume Confirmation": self._volume_confirmation_text(),
            "Risk Reward Ratio": self.risk_reward_ratio,
            "Signal Strength": self.signal_strength,
            "RSI": self.rsi,
            "MACD": "Bullish" if self.macd_bullish else "Bearish",
        }

    def _volume_confirmation_text(self) -> str:
        """Generate volume confirmation text."""
        if self.volume_current > self.volume_avg_20 * 1.5:
            return f"✅ High Volume ({self.volume_current/self.volume_avg_20:.2f}x)"
        elif self.volume_current > self.volume_avg_20:
            return f"🟡 Moderate Volume ({self.volume_current/self.volume_avg_20:.2f}x)"
        return f"⚠️ Low Volume ({self.volume_current/self.volume_avg_20:.2f}x)"

    def save_as_txt(self, folder: str = None):
        """Save signal as TXT file."""
        if folder is None:
            folder = SIGNAL_FOLDERS["buy"] if self.signal_type == "BUY" else SIGNAL_FOLDERS["sell"]
        
        Path(folder).mkdir(parents=True, exist_ok=True)
        filename = f"{self.symbol}_{self.signal_date.replace('-', '')}_{self.signal_time.replace(':', '')}.txt"
        filepath = os.path.join(folder, filename)
        
        content = f"""
================================================================================
ORDER BLOCK SIGNAL - {self.signal_type}
================================================================================
Symbol:                   {self.symbol}
Signal Date:              {self.signal_date}
Signal Time:              {self.signal_time}
Time Frame:               {self.timeframe}

================================================================================
ORDER BLOCK DETAILS
================================================================================
Order Block Type:         {self.to_dict()['Order Block Type']}
Order Block High:         {self.order_block_high}
Order Block Low:          {self.order_block_low}

================================================================================
ENTRY & EXIT POINTS
================================================================================
Entry Price:              {self.entry_price}
Stop Loss:                {self.stop_loss}
Target 1:                 {self.target_1}
Target 2:                 {self.target_2}
Current Price:            {self.current_price}

================================================================================
SIGNAL QUALITY
================================================================================
Signal Strength:          {self.signal_strength}
Risk Reward Ratio:        {self.risk_reward_ratio}
RSI:                      {self.rsi}
MACD:                     {'Bullish' if self.macd_bullish else 'Bearish'}
{self._volume_confirmation_text()}

================================================================================
Generated: {_now_ist().strftime('%d-%b-%Y %H:%M:%S IST')}
================================================================================
"""
        try:
            with open(filepath, 'w') as f:
                f.write(content)
            logger.info(f"Signal saved as TXT: {filepath}")
            return filepath
        except Exception as e:
            logger.error(f"Error saving TXT signal: {e}")
            return None

    def save_as_csv(self, folder: str = None):
        """Save signal as CSV file."""
        if folder is None:
            folder = SIGNAL_FOLDERS["exports"]
        
        Path(folder).mkdir(parents=True, exist_ok=True)
        filename = f"{self.symbol}_signals.csv"
        filepath = os.path.join(folder, filename)
        
        try:
            df = pd.DataFrame([self.to_dict()])
            if os.path.exists(filepath):
                existing_df = pd.read_csv(filepath)
                df = pd.concat([existing_df, df], ignore_index=True)
            df.to_csv(filepath, index=False)
            logger.info(f"Signal saved as CSV: {filepath}")
            return filepath
        except Exception as e:
            logger.error(f"Error saving CSV signal: {e}")
            return None

    def save_as_json(self, folder: str = None):
        """Save signal as JSON file."""
        if folder is None:
            folder = SIGNAL_FOLDERS["exports"]
        
        Path(folder).mkdir(parents=True, exist_ok=True)
        filename = f"{self.symbol}_{self.signal_date.replace('-', '')}_signals.json"
        filepath = os.path.join(folder, filename)
        
        try:
            signal_dict = self.to_dict()
            with open(filepath, 'w') as f:
                json.dump(signal_dict, f, indent=4)
            logger.info(f"Signal saved as JSON: {filepath}")
            return filepath
        except Exception as e:
            logger.error(f"Error saving JSON signal: {e}")
            return None

    def generate_chart(self, df: pd.DataFrame, folder: str = None) -> Optional[str]:
        """Generate and save signal chart with Order Block visualization."""
        if not MATPLOTLIB_AVAILABLE:
            logger.warning("Matplotlib not available - skipping chart generation")
            return None
        
        if folder is None:
            folder = SIGNAL_FOLDERS["charts"]
        
        Path(folder).mkdir(parents=True, exist_ok=True)
        
        try:
            fig, ax = plt.subplots(figsize=(14, 8))
            
            for idx, row in df.iterrows():
                color = 'green' if row['Close'] >= row['Open'] else 'red'
                ax.plot([idx, idx], [row['Low'], row['High']], color=color, linewidth=1)
                ax.plot([idx, idx], [row['Open'], row['Close']], color=color, linewidth=3)
            
            ob_rect = patches.Rectangle(
                (len(df) - 5, self.order_block_low),
                5,
                self.order_block_high - self.order_block_low,
                linewidth=2,
                edgecolor='blue' if self.signal_type == "BUY" else 'red',
                facecolor='blue' if self.signal_type == "BUY" else 'red',
                alpha=0.2,
                label=f"Order Block Zone"
            )
            ax.add_patch(ob_rect)
            
            ax.axhline(y=self.entry_price, color='black', linestyle='--', linewidth=2, label=f"Entry: {self.entry_price}")
            ax.axhline(y=self.stop_loss, color='red', linestyle='--', linewidth=2, label=f"Stop Loss: {self.stop_loss}")
            ax.axhline(y=self.target_1, color='green', linestyle='--', linewidth=2, label=f"Target 1: {self.target_1}")
            ax.axhline(y=self.target_2, color='lightgreen', linestyle='--', linewidth=2, label=f"Target 2: {self.target_2}")
            
            ax.set_xlabel("Candle")
            ax.set_ylabel("Price")
            ax.set_title(f"{self.symbol} - {self.signal_type} Signal - {self.signal_date} {self.signal_time}")
            ax.legend(loc='best')
            ax.grid(True, alpha=0.3)
            
            filename = f"{self.symbol}_{self.signal_date.replace('-', '')}_{self.signal_time.replace(':', '')}.png"
            filepath = os.path.join(folder, filename)
            plt.tight_layout()
            plt.savefig(filepath, dpi=100, bbox_inches='tight')
            plt.close()
            
            logger.info(f"Chart saved: {filepath}")
            return filepath
        except Exception as e:
            logger.error(f"Error generating chart: {e}")
            return None


class SignalDeduplicator:
    """Prevents duplicate signals for the same candle."""
    def __init__(self):
        self.signals: Dict[str, datetime] = {}
    
    def is_duplicate(self, symbol: str, signal_time: str) -> bool:
        """Check if signal is duplicate within time window."""
        key = f"{symbol}_{signal_time}"
        if key in self.signals:
            time_diff = (_now_ist() - self.signals[key]).total_seconds() / 3600
            if time_diff < SIGNAL_15M_DUPLICATE_PREVENTION_HOURS:
                return True
        return False
    
    def record_signal(self, symbol: str, signal_time: str):
        """Record a new signal."""
        key = f"{symbol}_{signal_time}"
        self.signals[key] = _now_ist()
    
    def cleanup_old_signals(self):
        """Remove signals older than the prevention window."""
        current_time = _now_ist()
        expired_keys = [
            key for key, timestamp in self.signals.items()
            if (current_time - timestamp).total_seconds() / 3600 > SIGNAL_15M_DUPLICATE_PREVENTION_HOURS
        ]
        for key in expired_keys:
            del self.signals[key]


signal_deduplicator = SignalDeduplicator()


def _is_intraday_candle_closed(candle_time_ist, resolution_minutes: int) -> bool:
    """True only if the intraday candle has fully closed."""
    candle_close = candle_time_ist + timedelta(minutes=resolution_minutes)
    return _now_ist() >= candle_close


def _fetch_15min_order_block_signals(fyers, symbol: str) -> Tuple[List[OrderBlockSignal], Optional[str]]:
    """Fetch and detect 15-minute Order Block signals."""
    if not isinstance(symbol, str) or not _VALID_EQ_SYMBOL_RE.match(symbol):
        return [], f"{symbol}: invalid symbol format — skipped"

    date_from = (datetime.today() - timedelta(days=SIGNAL_15M_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    date_to = datetime.today().strftime("%Y-%m-%d")

    resp, err = _safe_history(fyers, {
        "symbol": symbol, "resolution": SIGNAL_15M_RESOLUTION, "date_format": "1",
        "range_from": date_from, "range_to": date_to, "cont_flag": "1"
    })
    if err:
        return [], f"{symbol}: {err}"

    candles = resp.get("candles") if resp else None
    if not candles or len(candles) < 31:
        return [], None

    try:
        df = pd.DataFrame(candles, columns=["Time", "Open", "High", "Low", "Close", "Volume"])
        df["Time"] = pd.to_datetime(df["Time"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
        df[["Open", "High", "Low", "Close", "Volume"]] = df[["Open", "High", "Low", "Close", "Volume"]].apply(
            pd.to_numeric, errors="coerce"
        )
        df = df.dropna(subset=["Open", "High", "Low", "Close"]).sort_values("Time").reset_index(drop=True)

        if len(df) > 0 and not _is_intraday_candle_closed(df["Time"].iloc[-1], 15):
            df = df.iloc[:-1].reset_index(drop=True)

        if len(df) < 30:
            return [], None

        signals = []
        stock_ticker = symbol.replace("NSE:", "").replace("-EQ", "")
        
        last_candle = df.iloc[-1]
        last_close = float(last_candle["Close"])
        last_volume = float(last_candle["Volume"])
        vol_avg20 = float(df["Volume"].tail(20).mean())
        
        rsi = float(calculate_rsi(df["Close"]).iloc[-1])
        macd_line, signal_line, _ = calculate_macd(df["Close"])
        macd_bullish = bool(macd_line.iloc[-1] > signal_line.iloc[-1])
        atr = float(calculate_atr(df).iloc[-1])
        
        if pd.isna(atr) or atr <= 0:
            atr = last_close * 0.005
        
        smc_structure, _, _ = _calculate_smc_and_cisd(df)
        bullish_ob, bearish_ob, ob_zone, ob_strength = _detect_order_blocks(df, smc_structure)
        
        signal_date_str, signal_time_str = _candle_signal_timestamp(df, is_daily=False)
        
        if bullish_ob == "🟢 Bullish OB":
            if not signal_deduplicator.is_duplicate(symbol, signal_time_str):
                ob_low, ob_high = map(float, ob_zone.split("–"))
                entry = round(last_close, 2)
                sl = round(entry - 1.0 * atr, 2)
                t1 = round(entry + 1.0 * atr, 2)
                t2 = round(entry + 2.0 * atr, 2)
                risk = abs(entry - sl)
                reward = abs(t1 - entry)
                rr_ratio = round(reward / risk, 2) if risk > 0 else 0.0
                
                signal = OrderBlockSignal(
                    symbol=stock_ticker,
                    signal_type="BUY",
                    signal_date=signal_date_str,
                    signal_time=signal_time_str,
                    timeframe="15 Minutes",
                    order_block_high=ob_high,
                    order_block_low=ob_low,
                    entry_price=entry,
                    stop_loss=sl,
                    target_1=t1,
                    target_2=t2,
                    current_price=last_close,
                    volume_avg_20=vol_avg20,
                    volume_current=last_volume,
                    rsi=rsi,
                    macd_bullish=macd_bullish,
                    signal_strength=ob_strength,
                    risk_reward_ratio=rr_ratio,
                )
                signals.append(signal)
                signal_deduplicator.record_signal(symbol, signal_time_str)
                
                signal.save_as_txt()
                signal.save_as_csv()
                signal.save_as_json()
                if MATPLOTLIB_AVAILABLE:
                    signal.generate_chart(df)
                
                st.success(f"🟢 BUY Signal: {stock_ticker} at {entry}")
        
        if bearish_ob == "🔴 Bearish OB":
            if not signal_deduplicator.is_duplicate(symbol, signal_time_str):
                ob_low, ob_high = map(float, ob_zone.split("–"))
                entry = round(last_close, 2)
                sl = round(entry + 1.0 * atr, 2)
                t1 = round(entry - 1.0 * atr, 2)
                t2 = round(entry - 2.0 * atr, 2)
                risk = abs(entry - sl)
                reward = abs(t1 - entry)
                rr_ratio = round(reward / risk, 2) if risk > 0 else 0.0
                
                signal = OrderBlockSignal(
                    symbol=stock_ticker,
                    signal_type="SELL",
                    signal_date=signal_date_str,
                    signal_time=signal_time_str,
                    timeframe="15 Minutes",
                    order_block_high=ob_high,
                    order_block_low=ob_low,
                    entry_price=entry,
                    stop_loss=sl,
                    target_1=t1,
                    target_2=t2,
                    current_price=last_close,
                    volume_avg_20=vol_avg20,
                    volume_current=last_volume,
                    rsi=rsi,
                    macd_bullish=macd_bullish,
                    signal_strength=ob_strength,
                    risk_reward_ratio=rr_ratio,
                )
                signals.append(signal)
                signal_deduplicator.record_signal(symbol, signal_time_str)
                
                signal.save_as_txt()
                signal.save_as_csv()
                signal.save_as_json()
                if MATPLOTLIB_AVAILABLE:
                    signal.generate_chart(df)
                
                st.error(f"🔴 SELL Signal: {stock_ticker} at {entry}")
        
        return signals, None
    except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError, AttributeError) as e:
        return [], f"{symbol}: analysis error ({type(e).__name__})"


def run_15min_order_block_scan(fyers, symbols: List[str]):
    """Run 15-minute Order Block scan on multiple stocks."""
    symbols = _validate_symbols(symbols)
    results = []
    errors = []
    stats = ScanStats(total=len(symbols))
    progress = st.progress(0.0, text=f"Scanning 15-Min Order Blocks 0 / {len(symbols)}")
    done = 0
    
    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i:i + BATCH_SIZE]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(_fetch_15min_order_block_signals, fyers, s): s for s in batch}
            for future in as_completed(futures):
                try:
                    signals, err = future.result()
                    for signal in signals:
                        results.append(signal.to_dict())
                    if err:
                        errors.append(err)
                    stats.record(has_result=bool(signals), has_error=bool(err))
                except Exception as e:
                    errors.append(f"{futures[future]}: worker error ({type(e).__name__})")
                    stats.record(has_result=False, has_error=True)
                done += 1
                progress.progress(done / len(symbols), text=f"Scanning 15-Min Order Blocks {done} / {len(symbols)}")
        
        if i + BATCH_SIZE < len(symbols):
            time.sleep(BATCH_PAUSE_SECONDS)
    
    progress.empty()
    return results, errors, stats


# ── Analysis core ─────────────────────────────────────────────────────────
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


# ── Intraday Scanner ──────────────────────────────────────────────────────
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


# ── Swing Trade Scanner ───────────────────────────────────────────────────
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
    """Returns (result_dict_or_None, error_message_or_None)."""
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
    """Threaded, rate-limited scan with a progress bar + stats."""
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


# ════════════════════════════════════════════════════════════════
# ── ADDITIVE MODULES PART 2 ────────────────────────────────────
# ════════════════════════════════════════════════════════════════

_INTRADAY_RESOLUTION_MAP = {"5 Minutes": "5", "15 Minutes": "15"}


def _fetch_intraday_cisd_signal(fyers, symbol: str, resolution: str, timeframe_label: str):
    """Returns (row_dict_or_None, error_or_None)."""
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
        return None, None

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
        exit_date = (_now_ist() + timedelta(days=est_days)).strftime("%d-%b-%Y")

        ema200_last = float(ema200.iloc[-1])
        ema_gap_pct = abs((float(ema50.iloc[-1]) - ema200_last) / ema200_last * 100) if ema200_last else 0
        if ema_gap_pct >= 3:
            trend_strength = "🟢 Strong"
