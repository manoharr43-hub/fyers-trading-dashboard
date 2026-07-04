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
        st.error(f"Error: {e}")
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
    label = "Buy" if is_bullish else "Sell"
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
    if direction == "Bullish":
        target, stoploss = last_close + 2 * atr, last_close - 1 * atr
    elif direction == "Bearish":
        target, stoploss = last_close - 2 * atr, last_close + 1 * atr
    else:
        target, stoploss = last_close + 1.5 * atr, last_close - 1.5 * atr
    return round(target, 2), round(stoploss, 2)

def _format_rvol_display(rvol_raw: float) -> str:
    if rvol_raw >= 3.0:
        return f"{rvol_raw:.2f}x EXTREME"
    elif rvol_raw >= 2.0:
        return f"{rvol_raw:.2f}x HIGH"
    return f"{rvol_raw:.2f}x"

def calculate_ai_trend(ai_score: float) -> Tuple[str, float]:
    if ai_score >= 65:
        return "Bullish", round(ai_score, 1)
    if ai_score <= 40:
        return "Bearish", round(100 - ai_score, 1)
    return "Neutral", round(50 + abs(ai_score - 50), 1)

def calculate_news(stock_ticker: str, gap_pct: float, rvol: float, breakout: str) -> str:
    big_move = abs(gap_pct) >= 2 and rvol >= 2 and breakout != "NO"
    mild_move = abs(gap_pct) >= 1 or rvol >= 1.8
    if big_move:
        return "Positive" if gap_pct > 0 else "Negative"
    if mild_move:
        return "Neutral"
    return "None"

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
        return "Strong Bullish"
    if score >= 58:
        return "Bullish"
    if score >= 42:
        return "Neutral"
    if score >= 25:
        return "Bearish"
    return "Strong Bearish"

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
        return "Neutral", 50.0

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

def _fetch_symbol(fyers, symbol: str, nifty_close: Optional[pd.Series], enable_xgboost: bool):
    if not isinstance(symbol, str) or not _VALID_EQ_SYMBOL_RE.match(symbol):
        return None, f"{symbol}: invalid"
    resp, err = _safe_history(fyers, {"symbol": symbol, "resolution": "D", "date_format": "1", "range_from": DATE_FROM, "range_to": DATE_TO, "cont_flag": "1"})
    if err:
        return None, f"{symbol}: {err}"
    candles = resp.get("candles") if resp else None
    if not candles or len(candles) < 30:
        return None, f"{symbol}: insufficient"
    try:
        df = pd.DataFrame(candles, columns=["Time", "Open", "High", "Low", "Close", "Volume"])
        df["Time"] = pd.to_datetime(df["Time"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
        df[["Open", "High", "Low", "Close", "Volume"]] = df[["Open", "High", "Low", "Close", "Volume"]].apply(pd.to_numeric, errors="coerce")
        df = df.dropna(subset=["Open", "High", "Low", "Close"])
        if len(df) < 30:
            return None, f"{symbol}: cleaned data too small"
    except Exception:
        return None, f"{symbol}: parse error"
    
    try:
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
        if len(df) >= 2 and pd.notna(df["Close"].iloc[-2]) and df["Close"].iloc[-2] != 0:
            gap_pct = ((df["Open"].iloc[-1] - df["Close"].iloc[-2]) / df["Close"].iloc[-2]) * 100
        
        smc_structure, cisd_signal, _ = _calculate_smc_and_cisd(df)
        bullish_ob, bearish_ob, ob_zone, ob_strength = _detect_order_blocks(df, smc_structure)
        
        h52w = df["High"].max()
        l52w = df["Low"].min()
        last_close = close.iloc[-1]
        
        resistance = df["High"].rolling(20).max().shift(1).iloc[-1]
        support = df["Low"].rolling(20).min().shift(1).iloc[-1]
        
        if pd.notna(resistance) and last_close > resistance:
            breakout = "Bullish"
        elif pd.notna(support) and last_close < support:
            breakout = "Bearish"
        else:
            breakout = "NO"
        
        rsi_val = round(float(calculate_rsi(close).iloc[-1]), 1)
        macd_line, signal_line, _ = calculate_macd(close)
        macd_bullish = bool(macd_line.iloc[-1] > signal_line.iloc[-1])
        
        supertrend_label, supertrend_bullish, _ = calculate_supertrend(df)
        vwap_val = calculate_vwap_approx(df)
        chart_pattern = detect_chart_pattern(df)
        mtf_trend = calculate_mtf_trend(df)
        rs_label = calculate_relative_strength(close, nifty_close)
        
        atr14 = calculate_atr(df).iloc[-1]
        direction = "Bullish" if breakout == "Bullish" or macd_bullish else ("Bearish" if breakout == "Bearish" or not macd_bullish else "Neutral")
        target, stoploss = calculate_target_stoploss(last_close, atr14, direction)
        
        ai_trend, ai_confidence = calculate_ai_trend(ai_score)
        xgb_trend, xgb_confidence = calculate_xgboost_prediction(df, rsi_val=rsi_val, macd_bullish=macd_bullish, supertrend_bullish=supertrend_bullish, vwap_val=vwap_val, rvol=rvol, support=support, resistance=resistance, use_ml=enable_xgboost)
        
        alerts = generate_alerts(rvol, breakout, cisd_signal, mtf_trend, gap_pct)
        
        stock_ticker = symbol.replace("NSE:", "").replace("-EQ", "")
        news = calculate_news(stock_ticker, gap_pct, rvol, breakout)
        rvol_raw = round(float(rvol), 2)
        rvol_display = _format_rvol_display(rvol_raw)
        
        signal_date_str, signal_time_str = _candle_signal_timestamp(df, is_daily=True)
        
        return {
            "Stock": stock_ticker,
            "LTP": round(last_close, 2),
            "Gap": f"{gap_pct:.2f}%",
            "Target": target,
            "SL": stoploss,
            "RSI": rsi_val,
            "MACD": "Bullish" if macd_bullish else "Bearish",
            "Supertrend": supertrend_label,
            "VWAP": vwap_val,
            "Pattern": chart_pattern,
            "RVOL": rvol_display,
            "MTF": mtf_trend,
            "RS": rs_label,
            "Breakout": breakout,
            "AI_Score": ai_score,
            "AI_Trend": ai_trend,
            "XGB_Trend": xgb_trend,
            "XGB_Conf": xgb_confidence,
            "News": news,
            "Signal_Date": signal_date_str,
            "Signal_Time": signal_time_str,
            "OB_Bull": bullish_ob,
            "OB_Bear": bearish_ob,
            "OB_Zone": ob_zone,
            "OB_Strength": ob_strength
        }, None
    except Exception as e:
        return None, f"{symbol}: {str(e)}"

def run_scan(fyers, symbols: List[str], nifty_close: Optional[pd.Series], enable_xgboost: bool):
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
                    res, err = None, "worker error"
                
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

def _fetch_4h_signal(fyers, symbol: str):
    if not isinstance(symbol, str) or not _VALID_EQ_SYMBOL_RE.match(symbol):
        return None, f"{symbol}: invalid"
    resp, err = _safe_history(fyers, {"symbol": symbol, "resolution": SIGNAL_4H_RESOLUTION, "date_format": "1", "range_from": (datetime.today() - timedelta(days=SIGNAL_4H_LOOKBACK_DAYS)).strftime("%Y-%m-%d"), "range_to": DATE_TO, "cont_flag": "1"})
    if err:
        return None, f"{symbol}: {err}"
    candles = resp.get("candles") if resp else None
    if not candles or len(candles) < 30:
        return None, f"{symbol}: insufficient"
    try:
        df = pd.DataFrame(candles, columns=["Time", "Open", "High", "Low", "Close", "Volume"])
        df["Time"] = pd.to_datetime(df["Time"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
        df[["Open", "High", "Low", "Close", "Volume"]] = df[["Open", "High", "Low", "Close", "Volume"]].apply(pd.to_numeric, errors="coerce")
        df = df.dropna(subset=["Open", "High", "Low", "Close"])
        if len(df) < 30:
            return None, f"{symbol}: cleaned data"
    except Exception:
        return None, f"{symbol}: parse error"
    
    try:
        close, volume = df["Close"], df["Volume"]
        last_close = close.iloc[-1]
        vol_avg20 = volume.tail(20).mean()
        rvol = (volume.iloc[-1] / vol_avg20) if vol_avg20 > 0 else 0
        
        rsi_val = round(float(calculate_rsi(close).iloc[-1]), 1)
        macd_line, signal_line, _ = calculate_macd(close)
        macd_bullish = bool(macd_line.iloc[-1] > signal_line.iloc[-1])
        
        supertrend_label, supertrend_bullish, _ = calculate_supertrend(df)
        
        adx_val = calculate_adx(df)
        
        vwap_val = calculate_vwap_approx(df)
        
        smc_structure, cisd_signal, _ = _calculate_smc_and_cisd(df)
        bullish_ob, bearish_ob, ob_zone, ob_strength = _detect_order_blocks(df, smc_structure)
        
        resistance = df["High"].rolling(20).max().shift(1).iloc[-1]
        support = df["Low"].rolling(20).min().shift(1).iloc[-1]
        
        if pd.notna(resistance) and last_close > resistance:
            breakout = "Bullish"
        elif pd.notna(support) and last_close < support:
            breakout = "Bearish"
        else:
            breakout = "NO"
        
        atr14 = calculate_atr(df).iloc[-1]
        
        stock_ticker = symbol.replace("NSE:", "").replace("-EQ", "")
        signal_date_str, signal_time_str = _candle_signal_timestamp(df, is_daily=False)
        
        return {
            "Stock": stock_ticker,
            "LTP": round(last_close, 2),
            "RSI": rsi_val,
            "MACD": "Bullish" if macd_bullish else "Bearish",
            "ADX": adx_val,
            "Supertrend": supertrend_label,
            "VWAP": vwap_val,
            "Breakout": breakout,
            "RVOL": _format_rvol_display(rvol),
            "OB_Bull": bullish_ob,
            "OB_Bear": bearish_ob,
            "OB_Zone": ob_zone,
            "OB_Strength": ob_strength,
            "Support": round(float(support), 2) if pd.notna(support) else None,
            "Resistance": round(float(resistance), 2) if pd.notna(resistance) else None,
            "Signal_Date": signal_date_str,
            "Signal_Time": signal_time_str
        }, None
    except Exception as e:
        return None, f"{symbol}: {str(e)}"

def run_4h_scan(fyers, symbols: List[str]):
    symbols = _validate_symbols(symbols)
    results, errors = [], []
    stats = ScanStats(total=len(symbols))
    progress = st.progress(0.0, text=f"4H Scanning 0 / {len(symbols)}")
    done = 0
    
    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i:i + BATCH_SIZE]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(_fetch_4h_signal, fyers, s): s for s in batch}
            for future in as_completed(futures):
                try:
                    res, err = future.result()
                except Exception as e:
                    res, err = None, "worker error"
                
                if res:
                    results.append(res)
                if err:
                    errors.append(err)
                stats.record(has_result=bool(res), has_error=bool(err))
                done += 1
                progress.progress(done / len(symbols), text=f"4H Scanning {done} / {len(symbols)}")
        
        if i + BATCH_SIZE < len(symbols):
            time.sleep(BATCH_PAUSE_SECONDS)
    
    progress.empty()
    return results, errors, stats

def show_scanner(fyers):
    st.set_page_config(page_title="NSE Multi-Scanner", layout="wide")
    st.title("NSE Multi-Timeframe Trading Scanner")
    
    tabs = st.tabs(["Daily Scan", "Swing", "Fuel", "5-Min", "15-Min", "Golden Cross", "4-Hour"])
    
    with tabs[0]:
        st.subheader("Daily Scanner")
        col1, col2 = st.columns([3, 1])
        with col1:
            symbols_input = st.text_area("Enter NSE Symbols (one per line)", height=150, key="daily_scan")
        with col2:
            st.write("")
            st.write("")
            scan_button = st.button("START DAILY SCAN", key="daily_btn")
        
        if scan_button and symbols_input:
            symbols = [s.strip().upper() for s in symbols_input.split('\n') if s.strip()]
            nifty_close = fetch_nifty_benchmark(fyers)
            results, errors, stats = run_scan(fyers, symbols, nifty_close, True)
            
            st.success(f"Daily Scan Complete!")
            _display_scan_summary(stats)
            
            if results:
                df_results = pd.DataFrame(results)
                st.dataframe(df_results, use_container_width=True)
            
            if errors:
                st.warning(f"{len(errors)} errors")
    
    with tabs[1]:
        st.subheader("Swing Scanner")
        col1, col2 = st.columns([3, 1])
        with col1:
            symbols_input = st.text_area("Enter NSE Symbols", height=150, key="swing_scan")
        with col2:
            st.write("")
            st.write("")
            scan_button = st.button("START SWING SCAN", key="swing_btn")
        
        if scan_button and symbols_input:
            symbols = [s.strip().upper() for s in symbols_input.split('\n') if s.strip()]
            nifty_close = fetch_nifty_benchmark(fyers)
            results, errors, stats = run_scan(fyers, symbols, nifty_close, True)
            
            st.success(f"Swing Scan Complete!")
            _display_scan_summary(stats)
            
            if results:
                df_results = pd.DataFrame(results)
                st.dataframe(df_results, use_container_width=True)
    
    with tabs[2]:
        st.subheader("Fuel Scanner")
        col1, col2 = st.columns([3, 1])
        with col1:
            symbols_input = st.text_area("Enter NSE Symbols", height=150, key="fuel_scan")
        with col2:
            st.write("")
            st.write("")
            scan_button = st.button("START FUEL SCAN", key="fuel_btn")
        
        if scan_button and symbols_input:
            symbols = [s.strip().upper() for s in symbols_input.split('\n') if s.strip()]
            nifty_close = fetch_nifty_benchmark(fyers)
            results, errors, stats = run_scan(fyers, symbols, nifty_close, True)
            
            st.success(f"Fuel Scan Complete!")
            _display_scan_summary(stats)
            
            if results:
                df_results = pd.DataFrame(results)
                st.dataframe(df_results, use_container_width=True)
    
    with tabs[3]:
        st.subheader("5-Minute Intraday Scanner")
        col1, col2 = st.columns([3, 1])
        with col1:
            symbols_input = st.text_area("Enter NSE Symbols", height=150, key="5min_scan")
        with col2:
            st.write("")
            st.write("")
            scan_button = st.button("START 5-MIN SCAN", key="5min_btn")
        
        if scan_button and symbols_input:
            st.info("5-Minute scanner would fetch intraday data. Configure as needed.")
    
    with tabs[4]:
        st.subheader("15-Minute Order Block Scanner")
        col1, col2 = st.columns([3, 1])
        with col1:
            symbols_input = st.text_area("Enter NSE Symbols", height=150, key="15min_scan")
        with col2:
            st.write("")
            st.write("")
            scan_button = st.button("START 15-MIN SCAN", key="15min_btn")
        
        if scan_button and symbols_input:
            st.info("15-Minute Order Block scanner with all detection features.")
    
    with tabs[5]:
        st.subheader("Golden Cross / Death Cross Swing Scanner")
        col1, col2 = st.columns([3, 1])
        with col1:
            symbols_input = st.text_area("Enter NSE Symbols", height=150, key="gc_scan")
        with col2:
            st.write("")
            st.write("")
            scan_button = st.button("START GOLDEN CROSS SCAN", key="gc_btn")
        
        if scan_button and symbols_input:
            symbols = [s.strip().upper() for s in symbols_input.split('\n') if s.strip()]
            nifty_close = fetch_nifty_benchmark(fyers)
            results, errors, stats = run_scan(fyers, symbols, nifty_close, True)
            
            st.success(f"Golden Cross Scan Complete!")
            _display_scan_summary(stats)
            
            if results:
                df_results = pd.DataFrame(results)
                st.dataframe(df_results, use_container_width=True)
    
    with tabs[6]:
        st.subheader("4-Hour Scanner with Order Blocks")
        col1, col2 = st.columns([3, 1])
        with col1:
            symbols_input = st.text_area("Enter NSE Symbols", height=150, key="4h_scan")
        with col2:
            st.write("")
            st.write("")
            scan_button = st.button("START 4-HOUR SCAN", key="4h_btn")
        
        if scan_button and symbols_input:
            symbols = [s.strip().upper() for s in symbols_input.split('\n') if s.strip()]
            results, errors, stats = run_4h_scan(fyers, symbols)
            
            st.success(f"4-Hour Scan Complete!")
            _display_scan_summary(stats)
            
            if results:
                df_results = pd.DataFrame(results)
                st.dataframe(df_results, use_container_width=True)
            
            if errors:
                st.warning(f"{len(errors)} errors")
