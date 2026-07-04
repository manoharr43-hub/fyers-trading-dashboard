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

DATE_FROM = (datetime.today() - timedelta(days=365)).strftime("%Y-%m-%d")
DATE_TO = datetime.today().strftime("%Y-%m-%d")

FYERS_NSE_CM_SYMBOL_MASTER = "https://public.fyers.in/sym_details/NSE_CM.csv"
NIFTY_BENCHMARK_SYMBOL = "NSE:NIFTY50-INDEX"

MAX_WORKERS = 8
BATCH_SIZE = 50
BATCH_PAUSE_SECONDS = 1.0

XGB_MODEL_PATH = "xgb_trend_model.json"
INTRADAY_CISD_LOOKBACK_DAYS = 5

OB_LOOKBACK_CANDLES = 20
OB_MIN_VOLUME_MULTIPLIER = 1.2
OB_MIN_MOVE_PERCENT = 1.5

SIGNAL_15M_LOOKBACK_DAYS = 5
SIGNAL_15M_RESOLUTION = "15"
SIGNAL_15M_DUPLICATE_PREVENTION_HOURS = 4

SIGNAL_FOLDERS = {
    "base": "signals",
    "buy": "signals/buy",
    "sell": "signals/sell",
    "logs": "logs",
    "charts": "charts",
    "exports": "exports"
}

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def _ensure_signal_folders():
    try:
        for folder_path in SIGNAL_FOLDERS.values():
            Path(folder_path).mkdir(parents=True, exist_ok=True)
        logger.info(f"Signal folders created/verified: {list(SIGNAL_FOLDERS.values())}")
    except Exception as e:
        logger.error(f"Error creating signal folders: {e}")

_ensure_signal_folders()

def _now_ist() -> datetime:
    return datetime.now(IST)

from datetime import time as _dtime
_NSE_MARKET_CLOSE_IST = _dtime(15, 30, 0)

def _format_signal_timestamp(ts, is_daily: bool = False) -> Tuple[str, str]:
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    ts_ist = ts.tz_convert(IST)
    if is_daily:
        ts_ist = ts_ist.replace(hour=_NSE_MARKET_CLOSE_IST.hour, minute=_NSE_MARKET_CLOSE_IST.minute, second=_NSE_MARKET_CLOSE_IST.second, microsecond=0)
    return ts_ist.strftime("%d-%b-%Y"), ts_ist.strftime("%H:%M:%S") + " IST"

def _candle_signal_timestamp(df: pd.DataFrame, is_daily: bool = False) -> Tuple[str, str]:
    return _format_signal_timestamp(df["Time"].iloc[-1], is_daily=is_daily)

_HISTORY_MAX_RETRIES = 3
_HISTORY_BASE_DELAY_SECONDS = 1.0

def _safe_history(fyers, params: dict, max_retries: int = _HISTORY_MAX_RETRIES, base_delay: float = _HISTORY_BASE_DELAY_SECONDS) -> Tuple[Optional[dict], Optional[str]]:
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

def _validate_symbols(symbols: List[str]) -> List[str]:
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
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Total Stocks", stats.total)
    c2.metric("Scanned", stats.scanned)
    c3.metric("Successful", stats.successful)
    c4.metric("Skipped", stats.skipped)
    c5.metric("Failed", stats.failed)
    c6.metric("Scan Time", f"{stats.elapsed_seconds:.1f}s")

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
    sample = lines[: min(500, len(lines))]
    split_sample = [ln.split(",") for ln in sample]
    max_cols = max((len(p) for p in split_sample), default=0)
    best_col, best_hits = None, 0
    for col_idx in range(max_cols):
        hits = sum(1 for parts in split_sample if len(parts) > col_idx and parts[col_idx].strip().startswith("NSE:") and parts[col_idx].strip().endswith("-EQ"))
        if hits > best_hits:
            best_col, best_hits = col_idx, hits
    if best_col is None or best_hits == 0:
        st.error("Could not locate the trading-symbol column in the Fyers symbol master — the file format may have changed.")
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
        hits = sum(1 for parts in split_sample if len(parts) > col_idx and parts[col_idx].strip().startswith("NSE:"))
        if hits > best_hits:
            best_col, best_hits = col_idx, hits
    if best_col is None or best_hits == 0:
        st.error("Could not locate the trading-symbol column in the Fyers F&O symbol master — the file format may have changed.")
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
def fetch_nifty_benchmark(_fyers) -> Optional[pd.Series]:
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
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

def calculate_supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0) -> Tuple[str, Optional[bool], Optional[float]]:
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

def _format_rvol_display(rvol_raw: float) -> str:
    display = f"{rvol_raw:.2f}x"
    if rvol_raw >= 3.0:
        display += " 🔥🔥"
    elif rvol_raw >= 2.0:
        display += " ❤️‍🔥"
    return display

def calculate_ai_trend(ai_score: float) -> Tuple[str, float]:
    if ai_score >= 65:
        return "📈 Bullish", round(ai_score, 1)
    if ai_score <= 40:
        return "📉 Bearish", round(100 - ai_score, 1)
    return "➖ Neutral", round(50 + abs(ai_score - 50), 1)

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

def _rule_based_xgb_score(df: pd.DataFrame, rsi_val: float, macd_bullish: bool, supertrend_bullish: Optional[bool], vwap_val: float, rvol: float, support: float, resistance: float) -> float:
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

def calculate_xgboost_prediction(df: pd.DataFrame, rsi_val: Optional[float] = None, macd_bullish: Optional[bool] = None, supertrend_bullish: Optional[bool] = None, vwap_val: Optional[float] = None, rvol: Optional[float] = None, support: Optional[float] = None, resistance: Optional[float] = None, use_ml: bool = True) -> Tuple[str, float]:
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
        rule_score = _rule_based_xgb_score(df, rsi_val, macd_bullish, supertrend_bullish, vwap_val, rvol, support, resistance)
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
                        model = xgb.XGBClassifier(n_estimators=50, max_depth=3, learning_rate=0.1, eval_metric="logloss", verbosity=0)
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

def calculate_final_signal(ai_score: float, xgb_trend: str, mtf_trend: str, rs_label: str, rsi: float, macd_bullish: bool, supertrend_bullish: Optional[bool], breakout: str, cisd_signal: str, smc_structure: str) -> str:
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

SIGNAL_QUALITY_MIN_CONFIRMATIONS = 6

def _calculate_signal_quality(ema20: float, ema50: float, rsi_val: float, macd_bullish: bool, supertrend_bullish: Optional[bool], vwap_val: Optional[float], last_close: float, rvol_raw: float, breakout: str, cisd_signal: str, smc_structure: str, last_volume: float, vol_avg20: float) -> Tuple[str, int, bool, str, str]:
    rvol_ok = bool(rvol_raw and rvol_raw >= 1.5)
    volume_ok = bool(vol_avg20 and vol_avg20 > 0 and last_volume > vol_avg20)
    bull_checks = {"Bullish CISD": "Bullish" in cisd_signal, "BOS Confirmed": smc_structure in ("BOS 📈", "CHOCH 🐂"), "EMA20 > EMA50": ema20 > ema50, "MACD Bullish": macd_bullish is True, "Supertrend Buy": supertrend_bullish is True, "VWAP Support": vwap_val is not None and last_close > vwap_val, "RSI Bullish (50-80)": 50 < rsi_val < 80, "High RVOL": rvol_ok, "Breakout": breakout == "📈 Bullish", "Strong Volume": volume_ok}
    bear_checks = {"Bearish CISD": "Bearish" in cisd_signal, "CHOCH/BOS Down": smc_structure in ("BOS 📉", "CHOCH 🐻"), "EMA20 < EMA50": ema20 < ema50, "MACD Bearish": macd_bullish is False, "Supertrend Sell": supertrend_bullish is False, "VWAP Resistance": vwap_val is not None and last_close < vwap_val, "RSI Bearish (20-50)": 20 < rsi_val < 50, "High RVOL": rvol_ok, "Breakdown": breakout == "📉 Bearish", "Strong Volume": volume_ok}
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

def _determine_entry_and_decision(direction: str, confirmed_count: int, ai_score: float, confidence: float, rvol_raw: float, volume_ok: bool) -> Tuple[str, str, str]:
    trend_confirmed = confirmed_count >= SIGNAL_QUALITY_MIN_CONFIRMATIONS
    strict_buy = (direction == "BUY" and ai_score >= 80 and confidence >= 75 and rvol_raw >= 1.5 and volume_ok and trend_confirmed)
    strict_sell = (direction == "SELL" and ai_score <= 20 and confidence >= 75 and rvol_raw >= 1.5 and volume_ok and trend_confirmed)
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

def _calculate_smc_and_cisd(df: pd.DataFrame):
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

def _detect_order_blocks(df: pd.DataFrame, smc_structure: str) -> Tuple[str, str, str, str]:
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

class OrderBlockSignal:
    def __init__(self, symbol: str, signal_type: str, signal_date: str, signal_time: str, timeframe: str, order_block_high: float, order_block_low: float, entry_price: float, stop_loss: float, target_1: float, target_2: float, current_price: float, volume_avg_20: float, volume_current: float, rsi: float, macd_bullish: bool, signal_strength: str, risk_reward_ratio: float):
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
        return {"Symbol": self.symbol, "Signal Type": self.signal_type, "Signal Date": self.signal_date, "Signal Time": self.signal_time, "Time Frame": self.timeframe, "Order Block Type": "Bullish" if self.signal_type == "BUY" else "Bearish", "Order Block High": self.order_block_high, "Order Block Low": self.order_block_low, "Entry Price": self.entry_price, "Stop Loss": self.stop_loss, "Target 1": self.target_1, "Target 2": self.target_2, "Current Price": self.current_price, "Volume Confirmation": self._volume_confirmation_text(), "Risk Reward Ratio": self.risk_reward_ratio, "Signal Strength": self.signal_strength, "RSI": self.rsi, "MACD": "Bullish" if self.macd_bullish else "Bearish"}
    def _volume_confirmation_text(self) -> str:
        if self.volume_current > self.volume_avg_20 * 1.5:
            return f"✅ High Volume ({self.volume_current/self.volume_avg_20:.2f}x)"
        elif self.volume_current > self.volume_avg_20:
            return f"🟡 Moderate Volume ({self.volume_current/self.volume_avg_20:.2f}x)"
        return f"⚠️ Low Volume ({self.volume_current/self.volume_avg_20:.2f}x)"
    def save_as_txt(self, folder: str = None):
        if folder is None:
            folder = SIGNAL_FOLDERS["buy"] if self.signal_type == "BUY" else SIGNAL_FOLDERS["sell"]
        Path(folder).mkdir(parents=True, exist_ok=True)
        filename = f"{self.symbol}_{self.signal_date.replace('-', '')}_{self.signal_time.replace(':', '')}.txt"
        filepath = os.path.join(folder, filename)
        content = f"\n================================================================================\nORDER BLOCK SIGNAL - {self.signal_type}\n================================================================================\nSymbol: {self.symbol}\nSignal Date: {self.signal_date}\nSignal Time: {self.signal_time}\nTime Frame: {self.timeframe}\n\n================================================================================\nORDER BLOCK DETAILS\n================================================================================\nOrder Block Type: {self.to_dict()['Order Block Type']}\nOrder Block High: {self.order_block_high}\nOrder Block Low: {self.order_block_low}\n\n================================================================================\nENTRY & EXIT POINTS\n================================================================================\nEntry Price: {self.entry_price}\nStop Loss: {self.stop_loss}\nTarget 1: {self.target_1}\nTarget 2: {self.target_2}\nCurrent Price: {self.current_price}\n\n================================================================================\nSIGNAL QUALITY\n================================================================================\nSignal Strength: {self.signal_strength}\nRisk Reward Ratio: {self.risk_reward_ratio}\nRSI: {self.rsi}\nMACD: {'Bullish' if self.macd_bullish else 'Bearish'}\n{self._volume_confirmation_text()}\n\n================================================================================\nGenerated: {_now_ist().strftime('%d-%b-%Y %H:%M:%S IST')}\n================================================================================\n"
        try:
            with open(filepath, 'w') as f:
                f.write(content)*

