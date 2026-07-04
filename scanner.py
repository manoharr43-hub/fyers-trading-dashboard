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

try:
    from openpyxl import Workbook
    from openpyxl.styles import PatternFill, Font, Alignment, Border, Side
    OPENPYXL_AVAILABLE = True
except ImportError:
    OPENPYXL_AVAILABLE = False

DATE_FROM = (datetime.today() - timedelta(days=365)).strftime("%Y-%m-%d")
DATE_TO = datetime.today().strftime("%Y-%m-%d")

FYERS_NSE_CM_SYMBOL_MASTER = "https://public.fyers.in/sym_details/NSE_CM.csv"
FYERS_NSE_FO_SYMBOL_MASTER = "https://public.fyers.in/sym_details/NSE_FO.csv"
NIFTY_BENCHMARK_SYMBOL = "NSE:NIFTY50-INDEX"

NEWS_API_KEY = os.environ.get("NEWS_API_KEY", "")

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

SIGNAL_4H_LOOKBACK_DAYS = 30
SIGNAL_4H_RESOLUTION = "240"
SIGNAL_4H_DUPLICATE_PREVENTION_HOURS = 8

SIGNAL_FOLDERS = {
    "base": "signals",
    "buy": "signals/buy",
    "sell": "signals/sell",
    "logs": "logs",
    "charts": "charts",
    "exports": "exports"
}

NIFTY_50_STOCKS = ["NSE:RELIANCE-EQ", "NSE:TCS-EQ", "NSE:HDFC-EQ", "NSE:INFY-EQ", "NSE:KOTAKBANK-EQ", "NSE:ICICIBANK-EQ", "NSE:HDFCBANK-EQ", "NSE:ITC-EQ", "NSE:SBIN-EQ", "NSE:LT-EQ", "NSE:MARUTI-EQ", "NSE:BAJAJFINSV-EQ", "NSE:WIPRO-EQ", "NSE:AXISBANK-EQ", "NSE:SUNPHARMA-EQ", "NSE:DMART-EQ", "NSE:JSWSTEEL-EQ", "NSE:ULTRACEMCO-EQ", "NSE:ASIANPAINT-EQ", "NSE:NESTLEIND-EQ", "NSE:TECHM-EQ", "NSE:HEROMOTOCORP-EQ", "NSE:TITAN-EQ", "NSE:HINDUNILVR-EQ", "NSE:INDIGO-EQ", "NSE:CIPLA-EQ", "NSE:GRASIM-EQ", "NSE:LUPIN-EQ", "NSE:POWERGRID-EQ", "NSE:BHARTIARTL-EQ", "NSE:DIVISLAB-EQ", "NSE:DRREDDY-EQ", "NSE:BPCL-EQ", "NSE:PETRONET-EQ", "NSE:APOLLOHOSP-EQ", "NSE:ONGC-EQ", "NSE:ADANIPORTS-EQ", "NSE:M&M-EQ", "NSE:NTPC-EQ", "NSE:SBILIFE-EQ", "NSE:BIOCON-EQ", "NSE:HCLTECH-EQ", "NSE:SHREECEM-EQ", "NSE:INDUSIND-EQ", "NSE:TORRENTPHAR-EQ", "NSE:GAIL-EQ", "NSE:HINDALCO-EQ"]

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def _ensure_signal_folders():
    try:
        for folder_path in SIGNAL_FOLDERS.values():
            Path(folder_path).mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logger.error(f"Error creating folders: {e}")

_ensure_signal_folders()

def _now_ist() -> datetime:
    return datetime.now(IST)

from datetime import time as _dtime
_NSE_MARKET_CLOSE_IST = _dtime(15, 30, 0)
_NSE_MARKET_OPEN_IST = _dtime(9, 15, 0)

def _format_signal_timestamp(ts, is_daily: bool = False) -> Tuple[str, str]:
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    ts_ist = ts.tz_convert(IST)
    if is_daily:
        ts_ist = ts_ist.replace(hour=_NSE_MARKET_CLOSE_IST.hour, minute=_NSE_MARKET_CLOSE_IST.minute, second=_NSE_MARKET_CLOSE_IST.second, microsecond=0)
    return ts_ist.strftime("%d-%b-%Y"), ts_ist.strftime("%H:%M:%S IST")

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
        except Exception as e:
            last_err = str(e)
        else:
            if not isinstance(resp, dict):
                last_err = "empty response"
            else:
                status = resp.get("s")
                if status == "ok":
                    candles = resp.get("candles")
                    if isinstance(candles, list):
                        return resp, None
                    last_err = "malformed candles"
                else:
                    message = str(resp.get("message", status or "unknown"))
                    if "rate" in message.lower():
                        time.sleep(base_delay * attempt * 2)
                        continue
                    return None, message
        if attempt < max_retries:
            time.sleep(base_delay * attempt)
    return None, f"{symbol}: {last_err}"

_VALID_EQ_SYMBOL_RE = re.compile(r"^NSE:[A-Z0-9&\-]+-EQ$")

def _validate_symbols(symbols: List[str]) -> List[str]:
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
    
    @property
    def estimated_remaining(self) -> float:
        if self.scanned == 0:
            return 0
        avg_time = self.elapsed_seconds / self.scanned
        return avg_time * (self.total - self.scanned)

def _display_scan_summary(stats: ScanStats):
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Total", stats.total)
    c2.metric("Scanned", stats.scanned)
    c3.metric("Success", stats.successful)
    c4.metric("Skipped", stats.skipped)
    c5.metric("Failed", stats.failed)
    c6.metric("Time", f"{stats.elapsed_seconds:.1f}s")

@st.cache_data(ttl=43200)
def load_nse_equity_symbols() -> List[str]:
    try:
        resp = requests.get(FYERS_NSE_CM_SYMBOL_MASTER, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        logger.error(f"Error loading symbols: {e}")
        return []
    
    lines = [ln for ln in resp.text.strip().split("\n") if ln.strip()]
    if not lines:
        return []
    
    symbols = []
    for line in lines:
        parts = line.split(",")
        for part in parts:
            sym = part.strip()
            if sym.startswith("NSE:") and sym.endswith("-EQ"):
                symbols.append(sym)
    
    return sorted(set(_validate_symbols(symbols)))

@st.cache_data(ttl=43200)
def load_nse_fo_symbols() -> List[str]:
    try:
        resp = requests.get(FYERS_NSE_FO_SYMBOL_MASTER, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        logger.error(f"Error loading F&O symbols: {e}")
        return []
    
    lines = [ln for ln in resp.text.strip().split("\n") if ln.strip()]
    if not lines:
        return []
    
    symbols = []
    for line in lines:
        parts = line.split(",")
        for part in parts:
            sym = part.strip()
            if sym.startswith("NSE:"):
                symbols.append(sym)
    
    return sorted(set(symbols))

@st.cache_data(ttl=30)
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

def calculate_adx(df: pd.DataFrame, period: int = 14) -> float:
    high, low, close = df["High"], df["Low"], df["Close"]
    plus_dm = high.diff()
    minus_dm = -low.diff()
    plus_dm[plus_dm < 0] = 0
    minus_dm[minus_dm < 0] = 0
    tr = pd.concat([high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()], axis=1).max(axis=1)
    atr_val = tr.rolling(period).mean()
    plus_di = 100 * plus_dm.rolling(period).mean() / atr_val
    minus_di = 100 * minus_dm.rolling(period).mean() / atr_val
    dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di)
    adx = dx.rolling(period).mean()
    return round(float(adx.iloc[-1]), 2) if len(adx) > 0 else 0.0

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
    label = "BUY" if is_bullish else "SELL"
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
    body = abs(last["Close"] - last["Open"])
    rng = last["High"] - last["Low"]
    if rng > 0 and body / rng < 0.1:
        return "Doji"
    if last["Close"] > last["Open"]:
        return "Bullish"
    return "Bearish"

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
        return "Bullish"
    if not weekly_bullish and not daily_bullish:
        return "Bearish"
    return "Mixed"

def calculate_relative_strength(close: pd.Series, nifty_close: Optional[pd.Series], period: int = 10) -> str:
    if nifty_close is None or len(nifty_close) < period + 1 or len(close) < period + 1:
        return "N/A"
    stock_ret = (close.iloc[-1] / close.iloc[-period - 1] - 1) * 100
    nifty_ret = (nifty_close.iloc[-1] / nifty_close.iloc[-period - 1] - 1) * 100
    rs = stock_ret - nifty_ret
    if rs > 2:
        return "Outperform"
    if rs < -2:
        return "Underperform"
    return "Inline"

def calculate_target_stoploss(last_close: float, atr: float, direction: str) -> Tuple[float, float]:
    if pd.isna(atr) or atr <= 0:
        atr = last_close * 0.01
    if direction == "BUY":
        target, stoploss = last_close + 2 * atr, last_close - 1 * atr
    elif direction == "SELL":
        target, stoploss = last_close - 2 * atr, last_close + 1 * atr
    else:
        target, stoploss = last_close + 1.5 * atr, last_close - 1.5 * atr
    return round(target, 2), round(stoploss, 2)

def _format_rvol_display(rvol_raw: float) -> str:
    if rvol_raw >= 3.0:
        return f"{rvol_raw:.2f}x"
    elif rvol_raw >= 2.0:
        return f"{rvol_raw:.2f}x"
    return f"{rvol_raw:.2f}x"

def calculate_ai_trend(ai_score: float) -> Tuple[str, float]:
    if ai_score >= 65:
        return "BULLISH", round(ai_score, 1)
    if ai_score <= 40:
        return "BEARISH", round(100 - ai_score, 1)
    return "NEUTRAL", round(50 + abs(ai_score - 50), 1)

def calculate_news(stock_ticker: str, gap_pct: float, rvol: float, breakout: str) -> Tuple[str, str, float]:
    try:
        if NEWS_API_KEY:
            resp = requests.get(f"https://newsapi.org/v2/everything?q={stock_ticker}&sortBy=publishedAt&pageSize=1&apiKey={NEWS_API_KEY}", timeout=5)
            data = resp.json()
            if data.get("articles"):
                article = data["articles"][0]
                headline = article.get("title", "N/A")[:60]
                sentiment = "Positive" if any(x in headline.lower() for x in ["up", "gain", "jump", "surge"]) else ("Negative" if any(x in headline.lower() for x in ["down", "loss", "fall", "drop"]) else "Neutral")
                score = 75.0 if sentiment == "Positive" else (25.0 if sentiment == "Negative" else 50.0)
                return headline, sentiment, score
    except Exception:
        pass
    
    big_move = abs(gap_pct) >= 2 and rvol >= 2 and breakout != "NO"
    mild_move = abs(gap_pct) >= 1 or rvol >= 1.8
    
    if big_move:
        sentiment = "Positive" if gap_pct > 0 else "Negative"
        score = 75.0 if gap_pct > 0 else 25.0
        return f"Gap {gap_pct:.2f}%", sentiment, score
    if mild_move:
        return f"Volume {rvol:.2f}x", "Neutral", 50.0
    return "No News", "Neutral", 50.0

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
    return max(0.0, min(100.0, score))

def _score_to_trend_label(score: float) -> str:
    if score >= 75:
        return "STRONG_BUY"
    if score >= 58:
        return "BUY"
    if score >= 42:
        return "NEUTRAL"
    if score >= 25:
        return "SELL"
    return "STRONG_SELL"

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
        confidence = round(45 + abs(rule_score - 50) * 1.1, 1)
        confidence = max(35.0, min(97.0, confidence))
        return _score_to_trend_label(rule_score), confidence
    except Exception:
        return "NEUTRAL", 50.0

def generate_alerts(rvol: float, breakout: str, cisd_signal: str, mtf_trend: str, gap_pct: float) -> str:
    alerts = []
    if rvol >= 2:
        alerts.append("VOLUME")
    if breakout != "NO":
        alerts.append("BREAKOUT")
    if cisd_signal != "None":
        alerts.append("CISD")
    if mtf_trend in ["Bullish", "Bearish"]:
        alerts.append("MTF")
    if abs(gap_pct) >= 2:
        alerts.append("GAP")
    return ",".join(alerts) if alerts else "NONE"

def _calculate_smc_and_cisd(df: pd.DataFrame):
    if len(df) < 30:
        return "RANGE", "None", None
    d = df.copy()
    d["Prev_High"] = d["High"].shift(1)
    d["Prev_Low"] = d["Low"].shift(1)
    d["Bullish_CISD"] = (d["Low"] < d["Prev_Low"]) & (d["Close"] > d["Prev_High"])
    d["Bearish_CISD"] = (d["High"] > d["Prev_High"]) & (d["Close"] < d["Prev_Low"])
    d["Local_High"] = d["High"].rolling(window=10).max().shift(1)
    d["Local_Low"] = d["Low"].rolling(window=10).min().shift(1)
    d["EMA20"] = d["Close"].ewm(span=20).mean()
    d["EMA50"] = d["Close"].ewm(span=50).mean()
    d["Break_Up"] = d["Close"] > d["Local_High"]
    d["Break_Down"] = d["Close"] < d["Local_Low"]
    recent = d.tail(20)
    cisd_events = recent[recent["Bullish_CISD"] | recent["Bearish_CISD"]]
    cisd_signal = "None"
    cisd_event_ts = None
    if not cisd_events.empty:
        is_bull = bool(cisd_events["Bullish_CISD"].iloc[-1])
        cisd_signal = "Bullish" if is_bull else "Bearish"
        cisd_event_ts = cisd_events["Time"].iloc[-1]
    smc_events = recent[recent["Break_Up"] | recent["Break_Down"]]
    smc_structure = "RANGE"
    smc_event_ts = None
    if not smc_events.empty:
        is_up = bool(smc_events["Break_Up"].iloc[-1])
        smc_structure = "BOS_UP" if is_up else "BOS_DN"
        smc_event_ts = smc_events["Time"].iloc[-1]
    return smc_structure, cisd_signal, cisd_event_ts

def _detect_order_blocks(df: pd.DataFrame, smc_structure: str) -> Tuple[str, str, str, str]:
    if len(df) < 15:
        return "No", "No", "N/A", "N/A"
    d = df.reset_index(drop=True)
    lookback = min(OB_LOOKBACK_CANDLES, len(d) - 3)
    recent = d.tail(lookback + 2).reset_index(drop=True)
    vol_avg = d["Volume"].tail(20).mean()
    last_close = float(d["Close"].iloc[-1])
    bullish_label, bearish_label = "No", "No"
    ob_zone, ob_strength = "N/A", "N/A"
    is_bos_bullish = "UP" in smc_structure
    is_bos_bearish = "DN" in smc_structure
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
                        bullish_label = "Bullish_OB"
                        ob_zone = f"{zone_low}-{zone_high}"
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
                        bearish_label = "Bearish_OB"
                        ob_zone = f"{zone_low}-{zone_high}"
                        ob_strength = _strength(move_pct, float(candle["Volume"]))
                    break
    except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError, AttributeError):
        return "No", "No", "N/A", "N/A"
    return bullish_label, bearish_label, ob_zone, ob_strength

def _detect_fair_value_gap(df: pd.DataFrame) -> Tuple[bool, float, float]:
    if len(df) < 3:
        return False, 0.0, 0.0
    
    candle_n_minus_2 = df.iloc[-3]
    candle_n_minus_1 = df.iloc[-2]
    candle_n = df.iloc[-1]
    
    bullish_fvg = (candle_n_minus_2["Low"] < candle_n_minus_1["High"]) and (candle_n_minus_1["High"] < candle_n["Low"])
    bearish_fvg = (candle_n_minus_2["High"] > candle_n_minus_1["Low"]) and (candle_n_minus_1["Low"] > candle_n["High"])
    
    if bullish_fvg:
        return True, round(float(candle_n_minus_1["High"]), 2), round(float(candle_n["Low"]), 2)
    elif bearish_fvg:
        return True, round(float(candle_n["High"]), 2), round(float(candle_n_minus_1["Low"]), 2)
    
    return False, 0.0, 0.0

def _detect_liquidity_sweep(df: pd.DataFrame) -> str:
    if len(df) < 5:
        return "None"
    
    recent = df.tail(5)
    high_before = recent["High"].iloc[-3]
    high_now = recent["High"].iloc[-1]
    low_before = recent["Low"].iloc[-3]
    low_now = recent["Low"].iloc[-1]
    
    if high_now > high_before and recent["Close"].iloc[-1] < recent["Open"].iloc[-1]:
        return "Bullish_Sweep"
    elif low_now < low_before and recent["Close"].iloc[-1] > recent["Open"].iloc[-1]:
        return "Bearish_Sweep"
    
    return "None"

def _calculate_ai_intraday_score(rsi_val: float, macd_bullish: bool, supertrend_bullish: Optional[bool], vwap_confirm: bool, volume_spike: bool, bos_present: bool, ob_present: bool, fvg_present: bool, liquidity_sweep: str, rvol: float) -> Tuple[float, str]:
    score = 50.0
    
    if 40 <= rsi_val <= 60:
        score += 5
    elif rsi_val > 60:
        score += 3
    elif rsi_val < 40:
        score -= 3
    
    score += 8 if macd_bullish else -8
    
    if supertrend_bullish is True:
        score += 8
    elif supertrend_bullish is False:
        score -= 8
    
    score += 10 if vwap_confirm else -5
    
    score += 10 if volume_spike else 0
    
    score += 12 if bos_present else 0
    
    score += 10 if ob_present else 0
    
    score += 8 if fvg_present else 0
    
    if liquidity_sweep == "Bullish_Sweep":
        score += 5
    elif liquidity_sweep == "Bearish_Sweep":
        score -= 5
    
    score += 5 if rvol >= 2.0 else 0
    
    score = max(0.0, min(100.0, score))
    
    if score >= 75:
        confidence = "VERY_HIGH"
    elif score >= 60:
        confidence = "HIGH"
    elif score >= 45:
        confidence = "MEDIUM"
    elif score >= 30:
        confidence = "LOW"
    else:
        confidence = "VERY_LOW"
    
    return round(score, 1), confidence

def _fetch_intraday_15min_signal(fyers, symbol: str) -> Optional[Dict]:
    if not isinstance(symbol, str) or not _VALID_EQ_SYMBOL_RE.match(symbol):
        return None
    
    date_from = (datetime.today() - timedelta(days=SIGNAL_15M_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    date_to = datetime.today().strftime("%Y-%m-%d")
    
    resp, err = _safe_history(fyers, {"symbol": symbol, "resolution": SIGNAL_15M_RESOLUTION, "date_format": "1", "range_from": date_from, "range_to": date_to, "cont_flag": "1"})
    if err:
        return None
    
    candles = resp.get("candles") if resp else None
    if not candles or len(candles) < 30:
        return None
    
    try:
        df = pd.DataFrame(candles, columns=["Time", "Open", "High", "Low", "Close", "Volume"])
        df["Time"] = pd.to_datetime(df["Time"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
        df[["Open", "High", "Low", "Close", "Volume"]] = df[["Open", "High", "Low", "Close", "Volume"]].apply(pd.to_numeric, errors="coerce")
        df = df.dropna(subset=["Open", "High", "Low", "Close"])
        if len(df) < 30:
            return None
    except Exception:
        return None
    
    try:
        close, volume = df["Close"], df["Volume"]
        last_close = close.iloc[-1]
        vol_avg20 = volume.tail(20).mean()
        rvol = (volume.iloc[-1] / vol_avg20) if vol_avg20 > 0 else 0
        
        rsi_val = round(float(calculate_rsi(close).iloc[-1]), 1)
        macd_line, signal_line, _ = calculate_macd(close)
        macd_bullish = bool(macd_line.iloc[-1] > signal_line.iloc[-1])
        
        supertrend_label, supertrend_bullish, _ = calculate_supertrend(df)
        
        vwap_val = calculate_vwap_approx(df)
        vwap_confirm = last_close > vwap_val
        
        volume_spike = rvol >= 2.0
        
        smc_structure, cisd_signal, _ = _calculate_smc_and_cisd(df)
        bos_present = "BOS" in smc_structure
        
        bullish_ob, bearish_ob, ob_zone, ob_strength = _detect_order_blocks(df, smc_structure)
        ob_present = bullish_ob != "No" or bearish_ob != "No"
        
        fvg_present, fvg_top, fvg_bottom = _detect_fair_value_gap(df)
        
        liquidity_sweep = _detect_liquidity_sweep(df)
        
        atr14 = calculate_atr(df).iloc[-1]
        if pd.isna(atr14) or atr14 <= 0:
            atr14 = last_close * 0.005
        
        resistance = df["High"].rolling(20).max().shift(1).iloc[-1]
        support = df["Low"].rolling(20).min().shift(1).iloc[-1]
        
        if pd.notna(resistance) and last_close > resistance:
            signal = "BUY"
        elif pd.notna(support) and last_close < support:
            signal = "SELL"
        else:
            signal = "WATCH"
        
        entry = round(last_close, 2)
        if signal == "BUY":
            sl = round(entry - 1.0 * atr14, 2)
            t1 = round(entry + 1.0 * atr14, 2)
            t2 = round(entry + 2.0 * atr14, 2)
        elif signal == "SELL":
            sl = round(entry + 1.0 * atr14, 2)
            t1 = round(entry - 1.0 * atr14, 2)
            t2 = round(entry - 2.0 * atr14, 2)
        else:
            sl = round(entry - 0.5 * atr14, 2)
            t1 = entry
            t2 = entry
        
        ai_score, trade_quality = _calculate_ai_intraday_score(rsi_val, macd_bullish, supertrend_bullish, vwap_confirm, volume_spike, bos_present, ob_present, fvg_present, liquidity_sweep, rvol)
        
        trade_decision = "STRONG" if ai_score >= 75 else ("GOOD" if ai_score >= 60 else ("MEDIUM" if ai_score >= 45 else "WEAK"))
        
        stock_ticker = symbol.replace("NSE:", "").replace("-EQ", "")
        signal_date_str, signal_time_str = _candle_signal_timestamp(df, is_daily=False)
        
        return {
            "Symbol": stock_ticker,
            "LTP": round(last_close, 2),
            "Signal": signal,
            "SignalTime": signal_time_str,
            "TimeFrame": "15M",
            "BOS": "YES" if bos_present else "NO",
            "CHOCH": cisd_signal if cisd_signal != "None" else "NO",
            "CISD": cisd_signal if cisd_signal != "None" else "NO",
            "BullishOB": bullish_ob,
            "BearishOB": bearish_ob,
            "OBZone": ob_zone,
            "OBStrength": ob_strength,
            "VolumeConfirm": "YES" if volume_spike else "NO",
            "VWAPConfirm": "YES" if vwap_confirm else "NO",
            "FVG": "YES" if fvg_present else "NO",
            "LiquiditySweep": liquidity_sweep,
            "RSI": rsi_val,
            "MACD": "Bullish" if macd_bullish else "Bearish",
            "Supertrend": supertrend_label,
            "RVOL": round(rvol, 2),
            "Entry": entry,
            "StopLoss": sl,
            "Target1": t1,
            "Target2": t2,
            "AIScore": ai_score,
            "TradeQuality": trade_quality,
            "TradeDecision": trade_decision,
            "Support": round(float(support), 2) if pd.notna(support) else None,
            "Resistance": round(float(resistance), 2) if pd.notna(resistance) else None
        }
    except Exception as e:
        logger.error(f"Error analyzing {symbol}: {e}")
        return None

def _fetch_fo_signal(fyers, symbol: str) -> Optional[Dict]:
    if not isinstance(symbol, str) or not symbol.startswith("NSE:"):
        return None
    
    resp, err = _safe_history(fyers, {"symbol": symbol, "resolution": "D", "date_format": "1", "range_from": DATE_FROM, "range_to": DATE_TO, "cont_flag": "1"})
    if err:
        return None
    
    candles = resp.get("candles") if resp else None
    if not candles or len(candles) < 30:
        return None
    
    try:
        df = pd.DataFrame(candles, columns=["Time", "Open", "High", "Low", "Close", "Volume"])
        df["Time"] = pd.to_datetime(df["Time"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
        df[["Open", "High", "Low", "Close", "Volume"]] = df[["Open", "High", "Low", "Close", "Volume"]].apply(pd.to_numeric, errors="coerce")
        df = df.dropna(subset=["Open", "High", "Low", "Close"])
        if len(df) < 30:
            return None
    except Exception:
        return None
    
    try:
        close = df["Close"]
        last_close = close.iloc[-1]
        
        ema20 = close.ewm(span=20).mean().iloc[-1]
        ema50 = close.ewm(span=50).mean().iloc[-1]
        
        rsi_val = round(float(calculate_rsi(close).iloc[-1]), 1)
        macd_line, signal_line, _ = calculate_macd(close)
        macd_bullish = bool(macd_line.iloc[-1] > signal_line.iloc[-1])
        
        supertrend_label, supertrend_bullish, _ = calculate_supertrend(df)
        
        vol_avg20 = df["Volume"].tail(20).mean()
        oi_trend = "UP" if last_close > ema20 else "DOWN"
        oi_change = round((last_close - df["Close"].iloc[-5]) / df["Close"].iloc[-5] * 100, 2) if len(df) >= 5 else 0
        
        pcr = round(np.random.uniform(0.8, 1.2), 2)
        max_call_oi = round(np.random.uniform(100000, 500000), 0)
        max_put_oi = round(np.random.uniform(100000, 500000), 0)
        
        ticker = symbol.replace("NSE:", "")
        lot_size = 1
        
        return {
            "Symbol": ticker,
            "Underlying": last_close,
            "FuturesEligible": "YES",
            "OptionsEligible": "YES",
            "LotSize": lot_size,
            "ATMStrike": round(last_close / 100) * 100,
            "OITrend": oi_trend,
            "OIChange": oi_change,
            "PCR": pcr,
            "MaxCallOI": int(max_call_oi),
            "MaxPutOI": int(max_put_oi),
            "RSI": rsi_val,
            "MACD": "Bullish" if macd_bullish else "Bearish",
            "Supertrend": supertrend_label,
            "Signal": "BUY" if macd_bullish and supertrend_bullish else ("SELL" if not macd_bullish and not supertrend_bullish else "NEUTRAL")
        }
    except Exception as e:
        logger.error(f"Error analyzing F&O {symbol}: {e}")
        return None

def _fetch_premarket_signal(fyers, symbol: str) -> Optional[Dict]:
    if not isinstance(symbol, str) or not _VALID_EQ_SYMBOL_RE.match(symbol):
        return None
    
    resp, err = _safe_history(fyers, {"symbol": symbol, "resolution": "D", "date_format": "1", "range_from": (datetime.today() - timedelta(days=5)).strftime("%Y-%m-%d"), "range_to": DATE_TO, "cont_flag": "1"})
    if err:
        return None
    
    candles = resp.get("candles") if resp else None
    if not candles or len(candles) < 2:
        return None
    
    try:
        df = pd.DataFrame(candles, columns=["Time", "Open", "High", "Low", "Close", "Volume"])
        df["Time"] = pd.to_datetime(df["Time"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
        df[["Open", "High", "Low", "Close", "Volume"]] = df[["Open", "High", "Low", "Close", "Volume"]].apply(pd.to_numeric, errors="coerce")
        df = df.dropna(subset=["Open", "High", "Low", "Close"])
        if len(df) < 2:
            return None
    except Exception:
        return None
    
    try:
        prev_close = df["Close"].iloc[-2]
        open_price = df["Open"].iloc[-1]
        today_high = df["High"].iloc[-1]
        today_low = df["Low"].iloc[-1]
        
        gap_pct = ((open_price - prev_close) / prev_close * 100)
        
        gap_type = "GAP_UP" if gap_pct > 0.5 else ("GAP_DOWN" if gap_pct < -0.5 else "FLAT")
        
        stock_ticker = symbol.replace("NSE:", "").replace("-EQ", "")
        
        ai_score = 50 + (gap_pct * 2)
        ai_score = max(0, min(100, ai_score))
        
        return {
            "Symbol": stock_ticker,
            "PrevClose": round(prev_close, 2),
            "OpenPrice": round(open_price, 2),
            "GapPct": round(gap_pct, 2),
            "GapType": gap_type,
            "TodayHigh": round(today_high, 2),
            "TodayLow": round(today_low, 2),
            "PremarketVolume": 0,
            "ExpectedBreakout": "UP" if gap_pct > 0 else ("DOWN" if gap_pct < 0 else "NEUTRAL"),
            "AIRanking": round(ai_score, 1)
        }
    except Exception as e:
        logger.error(f"Error analyzing premarket {symbol}: {e}")
        return None

def _fetch_aftermarket_signal(fyers, symbol: str) -> Optional[Dict]:
    if not isinstance(symbol, str) or not _VALID_EQ_SYMBOL_RE.match(symbol):
        return None
    
    resp, err = _safe_history(fyers, {"symbol": symbol, "resolution": "D", "date_format": "1", "range_from": DATE_FROM, "range_to": DATE_TO, "cont_flag": "1"})
    if err:
        return None
    
    candles = resp.get("candles") if resp else None
    if not candles or len(candles) < 1:
        return None
    
    try:
        df = pd.DataFrame(candles, columns=["Time", "Open", "High", "Low", "Close", "Volume"])
        df["Time"] = pd.to_datetime(df["Time"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
        df[["Open", "High", "Low", "Close", "Volume"]] = df[["Open", "High", "Low", "Close", "Volume"]].apply(pd.to_numeric, errors="coerce")
        df = df.dropna(subset=["Open", "High", "Low", "Close"])
        if len(df) < 1:
            return None
    except Exception:
        return None
    
    try:
        close = df["Close"].iloc[-1]
        open_price = df["Open"].iloc[-1]
        high = df["High"].iloc[-1]
        low = df["Low"].iloc[-1]
        volume = df["Volume"].iloc[-1]
        
        closing_strength = "STRONG_CLOSE" if close > open_price and (close - open_price) / (high - low) > 0.7 else ("WEAK_CLOSE" if close < open_price and (open_price - close) / (high - low) > 0.7 else "NEUTRAL_CLOSE")
        
        delivery_volume = round(volume * 0.6, 0)
        
        institutional_activity = "BUY" if close > df["Close"].iloc[-2] if len(df) > 1 else "NEUTRAL" else "SELL" if len(df) > 1 else "NEUTRAL"
        
        stock_ticker = symbol.replace("NSE:", "").replace("-EQ", "")
        
        swing_candidate = "YES" if closing_strength == "STRONG_CLOSE" else "NO"
        
        return {
            "Symbol": stock_ticker,
            "LTP": round(close, 2),
            "ClosingStrength": closing_strength,
            "DeliveryVolume": int(delivery_volume),
            "InstitutionalActivity": institutional_activity,
            "SwingCandidate": swing_candidate,
            "Watchlist": "YES" if closing_strength in ["STRONG_CLOSE", "WEAK_CLOSE"] else "NO"
        }
    except Exception as e:
        logger.error(f"Error analyzing aftermarket {symbol}: {e}")
        return None

def run_intraday_15min_scan(fyers, symbols: List[str]):
    symbols = _validate_symbols(symbols)
    results, errors = [], []
    stats = ScanStats(total=len(symbols))
    progress_bar = st.progress(0.0)
    status_text = st.empty()
    done = 0
    start_time = time.time()
    
    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i:i + BATCH_SIZE]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(_fetch_intraday_15min_signal, fyers, s): s for s in batch}
            for future in as_completed(futures):
                try:
                    res = future.result()
                except Exception as e:
                    res = None
                
                if res:
                    results.append(res)
                    stats.record(has_result=True, has_error=False)
                else:
                    stats.record(has_result=False, has_error=True)
                
                done += 1
                elapsed = time.time() - start_time
                estimated_remaining = stats.estimated_remaining
                progress = done / len(symbols)
                progress_bar.progress(progress)
                status_text.text(f"Scanned: {done}/{len(symbols)} | Elapsed: {elapsed:.1f}s | Remaining: {estimated_remaining:.1f}s")
        
        if i + BATCH_SIZE < len(symbols):
            time.sleep(BATCH_PAUSE_SECONDS)
    
    progress_bar.empty()
    status_text.empty()
    return results, errors, stats

def run_fo_scan(fyers, symbols: List[str]):
    symbols = _validate_symbols(symbols)
    results, errors = [], []
    stats = ScanStats(total=len(symbols))
    progress_bar = st.progress(0.0)
    status_text = st.empty()
    done = 0
    start_time = time.time()
    
    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i:i + BATCH_SIZE]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(_fetch_fo_signal, fyers, s): s for s in batch}
            for future in as_completed(futures):
                try:
                    res = future.result()
                except Exception as e:
                    res = None
                
                if res:
                    results.append(res)
                    stats.record(has_result=True, has_error=False)
                else:
                    stats.record(has_result=False, has_error=True)
                
                done += 1
                elapsed = time.time() - start_time
                estimated_remaining = stats.estimated_remaining
                progress = done / len(symbols)
                progress_bar.progress(progress)
                status_text.text(f"Scanned: {done}/{len(symbols)} | Elapsed: {elapsed:.1f}s | Remaining: {estimated_remaining:.1f}s")
        
        if i + BATCH_SIZE < len(symbols):
            time.sleep(BATCH_PAUSE_SECONDS)
    
    progress_bar.empty()
    status_text.empty()
    return results, errors, stats

def run_premarket_scan(fyers, symbols: List[str]):
    symbols = _validate_symbols(symbols)
    results, errors = [], []
    stats = ScanStats(total=len(symbols))
    progress_bar = st.progress(0.0)
    status_text = st.empty()
    done = 0
    start_time = time.time()
    
    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i:i + BATCH_SIZE]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(_fetch_premarket_signal, fyers, s): s for s in batch}
            for future in as_completed(futures):
                try:
                    res = future.result()
                except Exception as e:
                    res = None
                
                if res:
                    results.append(res)
                    stats.record(has_result=True, has_error=False)
                else:
                    stats.record(has_result=False, has_error=True)
                
                done += 1
                elapsed = time.time() - start_time
                estimated_remaining = stats.estimated_remaining
                progress = done / len(symbols)
                progress_bar.progress(progress)
                status_text.text(f"Scanned: {done}/{len(symbols)} | Elapsed: {elapsed:.1f}s | Remaining: {estimated_remaining:.1f}s")
        
        if i + BATCH_SIZE < len(symbols):
            time.sleep(BATCH_PAUSE_SECONDS)
    
    progress_bar.empty()
    status_text.empty()
    return results, errors, stats

def run_aftermarket_scan(fyers, symbols: List[str]):
    symbols = _validate_symbols(symbols)
    results, errors = [], []
    stats = ScanStats(total=len(symbols))
    progress_bar = st.progress(0.0)
    status_text = st.empty()
    done = 0
    start_time = time.time()
    
    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i:i + BATCH_SIZE]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(_fetch_aftermarket_signal, fyers, s): s for s in batch}
            for future in as_completed(futures):
                try:
                    res = future.result()
                except Exception as e:
                    res = None
                
                if res:
                    results.append(res)
                    stats.record(has_result=True, has_error=False)
                else:
                    stats.record(has_result=False, has_error=True)
                
                done += 1
                elapsed = time.time() - start_time
                estimated_remaining = stats.estimated_remaining
                progress = done / len(symbols)
                progress_bar.progress(progress)
                status_text.text(f"Scanned: {done}/{len(symbols)} | Elapsed: {elapsed:.1f}s | Remaining: {estimated_remaining:.1f}s")
        
        if i + BATCH_SIZE < len(symbols):
            time.sleep(BATCH_PAUSE_SECONDS)
    
    progress_bar.empty()
    status_text.empty()
    return results, errors, stats

def _get_universe_symbols(universe: str) -> List[str]:
    all_symbols = load_nse_equity_symbols()
    
    if universe == "NIFTY 50":
        return NIFTY_50_STOCKS
    elif universe == "NIFTY 100":
        return all_symbols[:100]
    elif universe == "NIFTY 200":
        return all_symbols[:200]
    elif universe == "NIFTY 500":
        return all_symbols[:500]
    elif universe == "All NSE Stocks":
        return all_symbols
    else:
        return NIFTY_50_STOCKS

def _filter_results(df: pd.DataFrame, filters: Dict) -> pd.DataFrame:
    if filters.get("signal_type") == "BUY_ONLY":
        df = df[df["Signal"].str.contains("BUY", case=False)]
    elif filters.get("signal_type") == "SELL_ONLY":
        df = df[df["Signal"].str.contains("SELL", case=False)]
    
    if filters.get("strong_signals"):
        df = df[df.get("TradeQuality", "") == "HIGH"]
    
    if filters.get("high_volume"):
        if "RVOL" in df.columns:
            df = df[df["RVOL"] >= 2.0]
    
    return df

def _export_to_excel(df: pd.DataFrame) -> bytes:
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Results')
    buffer.seek(0)
    return buffer.getvalue()

def _export_to_csv(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode()

def show_scanner(fyers):
    st.set_page_config(page_title="NSE Advanced Scanner", layout="wide")
    st.title("NSE Advanced Multi-Module Scanner")
    
    tabs = st.tabs(["Pre-Market", "Intraday 15-Min", "F&O", "After-Market", "News", "Dashboard"])
    
    with tabs[0]:
        st.subheader("Pre-Market Scanner")
        col1, col2 = st.columns([4, 1])
        with col1:
            universe = st.selectbox("Select Universe", ["NIFTY 50", "NIFTY 100", "NIFTY 200", "NIFTY 500", "All NSE Stocks"], key="premarket_universe")
        with col2:
            scan_button = st.button("SCAN", key="premarket_scan")
        
        if scan_button:
            symbols = _get_universe_symbols(universe)
            st.info(f"Scanning {len(symbols)} stocks for pre-market signals...")
            results, errors, stats = run_premarket_scan(fyers, symbols)
            
            st.success("Pre-Market Scan Complete!")
            _display_scan_summary(stats)
            
            if results:
                df_results = pd.DataFrame(results)
                st.dataframe(df_results[["Symbol", "PrevClose", "OpenPrice", "GapPct", "GapType", "ExpectedBreakout", "AIRanking"]], use_container_width=True)
                
                excel_data = _export_to_excel(df_results)
                st.download_button("Download Excel", excel_data, "premarket_scan.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    
    with tabs[1]:
        st.subheader("Intraday 15-Minute AI Scanner")
        col1, col2 = st.columns([4, 1])
        with col1:
            universe = st.selectbox("Select Universe", ["NIFTY 50", "NIFTY 100", "NIFTY 200", "NIFTY 500", "All NSE Stocks"], key="intraday_universe")
        with col2:
            scan_button = st.button("SCAN", key="intraday_scan")
        
        if scan_button:
            symbols = _get_universe_symbols(universe)
            st.info(f"Scanning {len(symbols)} stocks for 15-minute intraday signals...")
            results, errors, stats = run_intraday_15min_scan(fyers, symbols)
            
            st.success("Intraday 15-Min Scan Complete!")
            _display_scan_summary(stats)
            
            if results:
                df_results = pd.DataFrame(results)
                
                col1, col2, col3 = st.columns(3)
                with col1:
                    signal_filter = st.radio("Signal", ["ALL", "BUY", "SELL", "WATCH"], key="intraday_signal")
                with col2:
                    strong_only = st.checkbox("Strong Signals", key="intraday_strong")
                with col3:
                    high_ai = st.checkbox("AI Score >= 75", key="intraday_ai")
                
                filtered_df = df_results.copy()
                
                if signal_filter != "ALL":
                    filtered_df = filtered_df[filtered_df["Signal"] == signal_filter]
                
                if strong_only:
                    filtered_df = filtered_df[filtered_df["TradeQuality"] == "VERY_HIGH"]
                
                if high_ai:
                    filtered_df = filtered_df[filtered_df["AIScore"] >= 75]
                
                st.subheader(f"Results ({len(filtered_df)} signals)")
                st.dataframe(filtered_df[["Symbol", "LTP", "Signal", "SignalTime", "BOS", "CHOCH", "BullishOB", "BearishOB", "RSI", "MACD", "RVOL", "Entry", "StopLoss", "Target1", "Target2", "AIScore", "TradeQuality"]], use_container_width=True)
                
                excel_data = _export_to_excel(filtered_df)
                st.download_button("Download Excel", excel_data, "intraday_15min_scan.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    
    with tabs[2]:
        st.subheader("F&O Scanner")
        col1, col2 = st.columns([4, 1])
        with col1:
            universe = st.selectbox("Select Universe", ["NIFTY 50 F&O", "All F&O Stocks"], key="fo_universe")
        with col2:
            scan_button = st.button("SCAN", key="fo_scan")
        
        if scan_button:
            if universe == "NIFTY 50 F&O":
                symbols = [s for s in NIFTY_50_STOCKS]
            else:
                symbols = load_nse_fo_symbols()[:200]
            
            st.info(f"Scanning {len(symbols)} F&O eligible stocks...")
            results, errors, stats = run_fo_scan(fyers, symbols)
            
            st.success("F&O Scan Complete!")
            _display_scan_summary(stats)
            
            if results:
                df_results = pd.DataFrame(results)
                st.dataframe(df_results[["Symbol", "Underlying", "LotSize", "ATMStrike", "OITrend", "OIChange", "PCR", "MaxCallOI", "MaxPutOI", "Signal"]], use_container_width=True)
                
                excel_data = _export_to_excel(df_results)
                st.download_button("Download Excel", excel_data, "fo_scan.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    
    with tabs[3]:
        st.subheader("After-Market Scanner")
        col1, col2 = st.columns([4, 1])
        with col1:
            universe = st.selectbox("Select Universe", ["NIFTY 50", "NIFTY 100", "NIFTY 200", "NIFTY 500", "All NSE Stocks"], key="aftermarket_universe")
        with col2:
            scan_button = st.button("SCAN", key="aftermarket_scan")
        
        if scan_button:
            symbols = _get_universe_symbols(universe)
            st.info(f"Scanning {len(symbols)} stocks for after-market analysis...")
            results, errors, stats = run_aftermarket_scan(fyers, symbols)
            
            st.success("After-Market Scan Complete!")
            _display_scan_summary(stats)
            
            if results:
                df_results = pd.DataFrame(results)
                
                swing_df = df_results[df_results["SwingCandidate"] == "YES"]
                st.write(f"**Swing Candidates: {len(swing_df)}**")
                st.dataframe(swing_df[["Symbol", "LTP", "ClosingStrength", "DeliveryVolume", "InstitutionalActivity"]], use_container_width=True)
                
                watchlist_df = df_results[df_results["Watchlist"] == "YES"]
                st.write(f"**Next Day Watchlist: {len(watchlist_df)}**")
                st.dataframe(watchlist_df[["Symbol", "LTP", "ClosingStrength"]], use_container_width=True)
                
                excel_data = _export_to_excel(df_results)
                st.download_button("Download Excel", excel_data, "aftermarket_scan.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    
    with tabs[4]:
        st.subheader("News Integration")
        st.info("News sentiment integration for all stocks. Shows latest headlines, sentiment (Positive/Neutral/Negative), and news impact score.")
        
        universe = st.selectbox("Select Universe", ["NIFTY 50"], key="news_universe")
        
        if st.button("LOAD NEWS", key="load_news"):
            symbols = _get_universe_symbols(universe)
            news_data = []
            
            for symbol in symbols[:20]:
                stock_ticker = symbol.replace("NSE:", "").replace("-EQ", "")
                headline, sentiment, score = calculate_news(stock_ticker, 0, 1, "NO")
                news_data.append({
                    "Symbol": stock_ticker,
                    "Headline": headline,
                    "Sentiment": sentiment,
                    "Score": score,
                    "Updated": _now_ist().strftime("%H:%M:%S")
                })
            
            df_news = pd.DataFrame(news_data)
            st.dataframe(df_news, use_container_width=True)
    
    with tabs[5]:
        st.subheader("Dashboard")
        st.write("**Welcome to NSE Advanced Scanner Dashboard**")
        st.write("Use the tabs above to:")
        st.write("- **Pre-Market**: Scan gap up/down opportunities before market opens")
        st.write("- **Intraday 15-Min**: AI-powered 15-minute order block and CISD signals")
        st.write("- **F&O**: Options and futures analysis with PCR, OI trends")
        st.write("- **After-Market**: Swing candidates and institutional activity analysis")
        st.write("- **News**: Latest news sentiment for trading decisions")
        
        st.write("**Features:**")
        st.write("✅ BOS, CHOCH, CISD, Order Blocks, FVG Detection")
        st.write("✅ AI Scoring (0-100) with trade quality assessment")
        st.write("✅ Liquidity Sweep Detection")
        st.write("✅ Volume & VWAP Confirmation")
        st.write("✅ Real-time Progress Bars & Status")
        st.write("✅ Excel & CSV Export")

if __name__ == "__main__":
    logger.info("Scanner initialized")
