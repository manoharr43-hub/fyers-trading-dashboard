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

def _ensure_app_folders():
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

def _now_ist():
    return datetime.now(IST)

def _format_signal_timestamp(ts, is_daily=False):
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    ts_ist = ts.tz_convert(IST)
    if is_daily:
        ts_ist = ts_ist.replace(hour=_NSE_MARKET_CLOSE_IST.hour, minute=_NSE_MARKET_CLOSE_IST.minute, second=_NSE_MARKET_CLOSE_IST.second, microsecond=0)
    return ts_ist.strftime("%d-%b-%Y"), ts_ist.strftime("%H:%M:%S") + " IST"

def _candle_signal_timestamp(df, is_daily=False):
    return _format_signal_timestamp(df["Time"].iloc[-1], is_daily=is_daily)

_HISTORY_MAX_RETRIES = 3
_HISTORY_BASE_DELAY_SECONDS = 1.0

def _safe_history(fyers, params, max_retries=_HISTORY_MAX_RETRIES, base_delay=_HISTORY_BASE_DELAY_SECONDS):
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

def _validate_symbols(symbols):
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
    def __init__(self, total):
        self.total = total
        self.scanned = 0
        self.successful = 0
        self.skipped = 0
        self.failed = 0
        self._start = time.time()

    def record(self, has_result, has_error):
        self.scanned += 1
        if has_result: self.successful += 1
        elif has_error: self.failed += 1
        else: self.skipped += 1

    @property
    def elapsed_seconds(self):
        return time.time() - self._start

def _display_scan_summary(stats):
    c1,c2,c3,c4,c5,c6 = st.columns(6)
    c1.metric("Total Stocks", stats.total)
    c2.metric("Scanned", stats.scanned)
    c3.metric("Successful", stats.successful)
    c4.metric("Skipped", stats.skipped)
    c5.metric("Failed", stats.failed)
    c6.metric("Scan Time", f"{stats.elapsed_seconds:.1f}s")

@st.cache_data(ttl=60*60*12)
def load_nse_equity_symbols():
    try:
        resp = requests.get(FYERS_NSE_CM_SYMBOL_MASTER, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        st.error(f"Could not download Fyers symbol master: {e}")
        return []
    lines = [ln for ln in resp.text.strip().split("\n") if ln.strip()]
    if not lines: return []
    sample = lines[:min(500,len(lines))]
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
        if len(parts) <= best_col: continue
        sym = parts[best_col].strip()
        if sym.startswith("NSE:") and sym.endswith("-EQ"):
            symbols.append(sym)
    return sorted(set(_validate_symbols(symbols)))

FYERS_NSE_FO_SYMBOL_MASTER = "https://public.fyers.in/sym_details/NSE_FO.csv"
_FO_INDEX_UNDERLYINGS = {"NIFTY","BANKNIFTY","FINNIFTY","MIDCPNIFTY","NIFTYNXT50","NIFTYIT","NIFTYPSE","NIFTYINFRA","SENSEX","BANKEX","NIFTY50"}

@st.cache_data(ttl=60*60*12)
def load_nse_fo_stock_symbols():
    try:
        resp = requests.get(FYERS_NSE_FO_SYMBOL_MASTER, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        st.error(f"Could not download Fyers F&O symbol master: {e}")
        return []
    lines = [ln for ln in resp.text.strip().split("\n") if ln.strip()]
    if not lines: return []
    sample = lines[:min(500,len(lines))]
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
        if len(parts) <= best_col: continue
        sym = parts[best_col].strip()
        if not sym.startswith("NSE:"): continue
        body = sym[len("NSE:"):]
        m = re.match(r"^([A-Z&\-]+)", body)
        if not m: continue
        underlying = m.group(1).strip("-")
        if underlying and underlying not in _FO_INDEX_UNDERLYINGS:
            underlyings.add(underlying)
    equity_symbols = load_nse_equity_symbols()
    equity_lookup = {s.replace("NSE:","").replace("-EQ",""): s for s in equity_symbols}
    fo_stock_symbols = sorted({equity_lookup[u] for u in underlyings if u in equity_lookup})
    return sorted(set(_validate_symbols(fo_stock_symbols)))

@st.cache_data(ttl=60*30)
def fetch_nifty_benchmark(_fyers):
    try:
        resp, err = _safe_history(_fyers, {"symbol": NIFTY_BENCHMARK_SYMBOL, "resolution": "D", "date_format": "1", "range_from": DATE_FROM, "range_to": DATE_TO, "cont_flag": "1"})
        if err or not resp: return None
        candles = resp.get("candles")
        if not candles: return None
        ndf = pd.DataFrame(candles, columns=["Time","Open","High","Low","Close","Volume"])
        ndf["Time"] = pd.to_datetime(ndf["Time"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
        return ndf["Close"]
    except Exception:
        return None

def calculate_rsi(close, period=14):
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return (100 - (100/(1+rs))).fillna(50)

def calculate_macd(close):
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    return macd_line, signal_line, macd_line - signal_line

def calculate_atr(df, period=14):
    h, l, c = df["High"], df["Low"], df["Close"]
    pc = c.shift(1)
    tr = pd.concat([h-l, (h-pc).abs(), (l-pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, min_periods=period, adjust=False).mean()

def calculate_supertrend(df, period=10, multiplier=3.0):
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
        final_upper[i] = upperband[i] if (upperband[i] < final_upper[i-1] or close[i-1] > final_upper[i-1]) else final_upper[i-1]
        final_lower[i] = lowerband[i] if (lowerband[i] > final_lower[i-1] or close[i-1] < final_lower[i-1]) else final_lower[i-1]
        if supertrend[i-1] == final_upper[i-1]:
            if close[i] <= final_upper[i]: supertrend[i] = final_upper[i]; direction[i] = -1
            else: supertrend[i] = final_lower[i]; direction[i] = 1
        else:
            if close[i] >= final_lower[i]: supertrend[i] = final_lower[i]; direction[i] = 1
            else: supertrend[i] = final_upper[i]; direction[i] = -1
    is_bullish = bool(direction[-1] == 1)
    return ("🟢 Buy" if is_bullish else "🔴 Sell"), is_bullish, round(float(supertrend[-1]), 2)

def calculate_vwap_approx(df, window=20):
    d = df.tail(window)
    typical = (d["High"] + d["Low"] + d["Close"]) / 3
    vol_sum = d["Volume"].sum()
    if vol_sum <= 0: return round(float(d["Close"].iloc[-1]), 2)
    return round(float((typical * d["Volume"]).sum() / vol_sum), 2)

def detect_chart_pattern(df):
    if len(df) < 5: return "N/A"
    last = df.iloc[-1]; prev = df.iloc[-2]
    body = abs(last["Close"] - last["Open"])
    rng = last["High"] - last["Low"]
    upper_wick = last["High"] - max(last["Close"], last["Open"])
    lower_wick = min(last["Close"], last["Open"]) - last["Low"]
    if rng > 0 and body/rng < 0.1: return "Doji ⚪"
    if lower_wick > body*2 and last["Close"] > last["Open"]: return "Hammer 🔨"
    if upper_wick > body*2 and last["Close"] < last["Open"]: return "Shooting Star 🌠"
    prev_lo, prev_hi = min(prev["Open"],prev["Close"]), max(prev["Open"],prev["Close"])
    last_lo, last_hi = min(last["Open"],last["Close"]), max(last["Open"],last["Close"])
    if last["Close"]>last["Open"] and prev["Close"]<prev["Open"] and last_hi>=prev_hi and last_lo<=prev_lo: return "Bullish Engulfing 🟢"
    if last["Close"]<last["Open"] and prev["Close"]>prev["Open"] and last_hi>=prev_hi and last_lo<=prev_lo: return "Bearish Engulfing 🔴"
    recent = df.tail(5)
    if recent["High"].is_monotonic_increasing and recent["Low"].is_monotonic_increasing: return "Higher Highs/Lows 📈"
    if recent["High"].is_monotonic_decreasing and recent["Low"].is_monotonic_decreasing: return "Lower Highs/Lows 📉"
    return "No Clear Pattern"

def calculate_mtf_trend(df):
    d = df.set_index("Time")
    weekly = d["Close"].resample("W").last().dropna()
    if len(weekly) < 6: return "N/A"
    w_span = min(20, max(len(weekly)-1, 2))
    weekly_bullish = bool(weekly.iloc[-1] > weekly.ewm(span=w_span, adjust=False).mean().iloc[-1])
    daily_ema20 = df["Close"].ewm(span=20, adjust=False).mean().iloc[-1]
    daily_bullish = bool(df["Close"].iloc[-1] > daily_ema20)
    if weekly_bullish and daily_bullish: return "🟢 Aligned Bullish"
    if not weekly_bullish and not daily_bullish: return "🔴 Aligned Bearish"
    return "🟡 Mixed"

def calculate_relative_strength(close, nifty_close, period=10):
    if nifty_close is None or len(nifty_close) < period+1 or len(close) < period+1: return "N/A"
    stock_ret = (close.iloc[-1]/close.iloc[-period-1]-1)*100
    nifty_ret = (nifty_close.iloc[-1]/nifty_close.iloc[-period-1]-1)*100
    rs = stock_ret - nifty_ret
    if rs > 2: return f"🟢 Outperform ({rs:+.1f}%)"
    if rs < -2: return f"🔴 Underperform ({rs:+.1f}%)"
    return f"🟡 Inline ({rs:+.1f}%)"

def calculate_target_stoploss(last_close, atr, direction):
    if pd.isna(atr) or atr <= 0: atr = last_close * 0.01
    if direction == "Bullish": return round(last_close+2*atr,2), round(last_close-1*atr,2)
    elif direction == "Bearish": return round(last_close-2*atr,2), round(last_close+1*atr,2)
    return round(last_close+1.5*atr,2), round(last_close-1.5*atr,2)

def _format_rvol_display(rvol_raw):
    display = f"{rvol_raw:.2f}x"
    if rvol_raw >= 3.0: display += " 🔥🔥"
    elif rvol_raw >= 2.0: display += " ❤️‍🔥"
    return display

def calculate_ai_trend(ai_score):
    if ai_score >= 65: return "📈 Bullish", round(ai_score, 1)
    if ai_score <= 40: return "📉 Bearish", round(100-ai_score, 1)
    return "➖ Neutral", round(50+abs(ai_score-50), 1)

NEWS_API_ENABLED = bool(os.environ.get("NEWS_API_KEY"))

def fetch_news_sentiment_live(stock_ticker):
    return None

def calculate_news(stock_ticker, gap_pct, rvol, breakout):
    live = fetch_news_sentiment_live(stock_ticker)
    if live is not None: return live
    big_move = abs(gap_pct) >= 2 and rvol >= 2 and breakout != "NO"
    mild_move = abs(gap_pct) >= 1 or rvol >= 1.8
    if big_move: return "🟢 Positive News" if gap_pct > 0 else "🔴 Negative News"
    if mild_move: return "🟡 Neutral News"
    return "⚪ No Recent News"

def _rule_based_xgb_score(df, rsi_val, macd_bullish, supertrend_bullish, vwap_val, rvol, support, resistance):
    last_close = float(df["Close"].iloc[-1])
    score = 50.0
    if len(df) >= 10:
        roc = (last_close / float(df["Close"].iloc[-10]) - 1) * 100
        score += max(min(roc*2, 15), -15)
    score += (rsi_val - 50) * 0.3
    score += 8 if macd_bullish else -8
    if supertrend_bullish is True: score += 8
    elif supertrend_bullish is False: score -= 8
    if vwap_val: score += 5 if last_close > vwap_val else -5
    if rvol and rvol >= 2: score += 5 if score >= 50 else -5
    if pd.notna(resistance) and resistance > 0 and (resistance - last_close)/last_close < 0.02: score -= 4
    if pd.notna(support) and support > 0 and (last_close - support)/last_close < 0.02: score += 4
    return max(0.0, min(100.0, score))

def _score_to_trend_label(score):
    if score >= 75: return "🟢 Strong Bullish"
    if score >= 58: return "🟢 Bullish"
    if score >= 42: return "🟡 Neutral"
    if score >= 25: return "🔴 Bearish"
    return "🔴 Strong Bearish"

def calculate_xgboost_prediction(df, rsi_val=None, macd_bullish=None, supertrend_bullish=None, vwap_val=None, rvol=None, support=None, resistance=None, use_ml=True):
    try:
        close = df["Close"]
        if rsi_val is None: rsi_val = float(calculate_rsi(close).iloc[-1])
        if macd_bullish is None:
            ml, sl, _ = calculate_macd(close)
            macd_bullish = bool(ml.iloc[-1] > sl.iloc[-1])
        if supertrend_bullish is None: _, supertrend_bullish, _ = calculate_supertrend(df)
        if vwap_val is None: vwap_val = calculate_vwap_approx(df)
        if rvol is None:
            vol_avg20 = df["Volume"].tail(20).mean()
            rvol = (df["Volume"].iloc[-1]/vol_avg20) if vol_avg20 > 0 else 0
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
                d["Vol_Ratio"] = d["Volume"]/d["Volume"].rolling(20).mean()
                d["EMA_Dist"] = d["Close"]/d["Close"].ewm(span=20,adjust=False).mean()-1
                fc = ["Return","RSI","MACD_Hist","Vol_Ratio","EMA_Dist"]
                latest = d.dropna(subset=fc).iloc[[-1]]
                if not latest.empty:
                    proba = model.predict_proba(latest[fc])[0]
                    blended = 0.7*float(proba[1])*100 + 0.3*rule_score
                    return _score_to_trend_label(blended), round(float(max(proba))*100, 1)
            except Exception: pass
        if use_ml and XGBOOST_AVAILABLE and len(df) >= 100:
            try:
                d = df.copy().reset_index(drop=True)
                d["Return"] = d["Close"].pct_change(); d["RSI"] = calculate_rsi(d["Close"])
                _, _, hist = calculate_macd(d["Close"]); d["MACD_Hist"] = hist
                d["Vol_Ratio"] = d["Volume"]/d["Volume"].rolling(20).mean()
                d["EMA_Dist"] = d["Close"]/d["Close"].ewm(span=20,adjust=False).mean()-1
                d["Target"] = (d["Close"].shift(-1) > d["Close"]).astype(int)
                fc = ["Return","RSI","MACD_Hist","Vol_Ratio","EMA_Dist"]
                d = d.dropna(subset=fc)
                if len(d) >= 60:
                    train = d.iloc[:-1]; latest = d.iloc[[-1]]
                    X_train, y_train = train[fc], train["Target"]
                    if y_train.nunique() >= 2:
                        model = xgb.XGBClassifier(n_estimators=50, max_depth=3, learning_rate=0.1, eval_metric="logloss", verbosity=0)
                        model.fit(X_train, y_train)
                        proba = model.predict_proba(latest[fc])[0]
                        blended = 0.6*float(proba[1])*100 + 0.4*rule_score
                        return _score_to_trend_label(blended), round(float(max(proba))*100, 1)
            except Exception: pass
        confidence = max(35.0, min(97.0, round(45 + abs(rule_score-50)*1.1, 1)))
        return _score_to_trend_label(rule_score), confidence
    except Exception:
        return "🟡 Neutral", 50.0

def generate_alerts(rvol, breakout, cisd_signal, mtf_trend, gap_pct):
    alerts = []
    if rvol >= 2: alerts.append("🔥 Volume Spike")
    if breakout != "NO": alerts.append("🚀 Breakout")
    if cisd_signal != "None": alerts.append("⚡ CISD")
    if "Aligned" in mtf_trend: alerts.append("📊 MTF Aligned")
    if abs(gap_pct) >= 2: alerts.append("↕️ Big Gap")
    return ", ".join(alerts) if alerts else "—"

def calculate_final_signal(ai_score, xgb_trend, mtf_trend, rs_label, rsi, macd_bullish, supertrend_bullish, breakout, cisd_signal, smc_structure):
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
    if score >= 5: return "🟢 Strong Buy"
    if score >= 2: return "🔵 Buy"
    if score > -2: return "🟡 Wait"
    if score > -5: return "🟠 Sell"
    return "🔴 Strong Sell"

SIGNAL_QUALITY_MIN_CONFIRMATIONS = 6

def _calculate_signal_quality(ema20, ema50, rsi_val, macd_bullish, supertrend_bullish, vwap_val, last_close, rvol_raw, breakout, cisd_signal, smc_structure, last_volume, vol_avg20):
    rvol_ok = bool(rvol_raw and rvol_raw >= 1.5)
    volume_ok = bool(vol_avg20 and vol_avg20 > 0 and last_volume > vol_avg20)
    bull_checks = {
        "Bullish CISD": "Bullish" in cisd_signal,
        "BOS Confirmed": smc_structure in ("BOS 📈","CHOCH 🐂"),
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
        "CHOCH/BOS Down": smc_structure in ("BOS 📉","CHOCH 🐻"),
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
        reasons = [label for label,ok in bull_checks.items() if ok]
    else:
        direction = "SELL"; confirmed_count = bear_count
        reasons = [label for label,ok in bear_checks.items() if ok]
    is_high_quality = confirmed_count >= SIGNAL_QUALITY_MIN_CONFIRMATIONS
    if confirmed_count >= 10: star_rating = "★★★★★ Very Strong"
    elif confirmed_count >= 8: star_rating = "★★★★ Strong"
    elif confirmed_count >= 6: star_rating = "★★★ Medium"
    elif confirmed_count >= 4: star_rating = "★★ Weak"
    else: star_rating = "★ Very Weak"
    reason_str = ", ".join(reasons) if reasons else "No strong confluence"
    return direction, confirmed_count, is_high_quality, star_rating, reason_str

def _determine_entry_and_decision(direction, confirmed_count, ai_score, confidence, rvol_raw, volume_ok):
    trend_confirmed = confirmed_count >= SIGNAL_QUALITY_MIN_CONFIRMATIONS
    strict_buy = direction=="BUY" and ai_score>=80 and confidence>=75 and rvol_raw>=1.5 and volume_ok and trend_confirmed
    strict_sell = direction=="SELL" and ai_score<=20 and confidence>=75 and rvol_raw>=1.5 and volume_ok and trend_confirmed
    if strict_buy: return "✅ Confirmed BUY", "🟢 High Probability" if confirmed_count>=8 else "🟡 Medium Probability", "🟢 BUY"
    if strict_sell: return "❌ Avoid Trade", "🟢 High Probability" if confirmed_count>=8 else "🟡 Medium Probability", "🔴 SELL"
    tq = "🟢 High Probability" if confirmed_count>=8 else ("🟡 Medium Probability" if confirmed_count>=6 else "🔴 Low Probability")
    return "⚠️ Wait for Confirmation", tq, "🟡 WAIT"

def _calculate_smc_and_cisd(df):
    if len(df) < 30: return "Range ➖", "None", None
    d = df.copy()
    d["Prev_High"] = d["High"].shift(1); d["Prev_Low"] = d["Low"].shift(1)
    d["Bullish_CISD"] = (d["Low"] < d["Prev_Low"]) & (d["Close"] > d["Prev_High"])
    d["Bearish_CISD"] = (d["High"] > d["Prev_High"]) & (d["Close"] < d["Prev_Low"])
    d["Local_High"] = d["High"].rolling(window=10).max().shift(1)
    d["Local_Low"] = d["Low"].rolling(window=10).min().shift(1)
    d["EMA20"] = d["Close"].ewm(span=20).mean(); d["EMA50"] = d["Close"].ewm(span=50).mean()
    d["Bullish_Trend"] = d["EMA20"] > d["EMA50"]
    d["Break_Up"] = d["Close"] > d["Local_High"]; d["Break_Down"] = d["Close"] < d["Local_Low"]
    recent = d.tail(20)
    cisd_events = recent[recent["Bullish_CISD"] | recent["Bearish_CISD"]]
    cisd_signal = "None"; cisd_event_ts = None
    if not cisd_events.empty:
        is_bull = bool(cisd_events["Bullish_CISD"].iloc[-1])
        cisd_signal = "Bullish CISD 🚀" if is_bull else "Bearish CISD 🩸"
        cisd_event_ts = cisd_events["Time"].iloc[-1]
    smc_events = recent[recent["Break_Up"] | recent["Break_Down"]]
    smc_structure = "Range ➖"; smc_event_ts = None
    if not smc_events.empty:
        is_up = bool(smc_events["Break_Up"].iloc[-1])
        is_bull_trend = bool(smc_events["Bullish_Trend"].iloc[-1])
        if is_up: smc_structure = "BOS 📈" if is_bull_trend else "CHOCH 🐂"
        else: smc_structure = "BOS 📉" if not is_bull_trend else "CHOCH 🐻"
        smc_event_ts = smc_events["Time"].iloc[-1]
    event_ts = cisd_event_ts if cisd_event_ts is not None else smc_event_ts
    return smc_structure, cisd_signal, event_ts

_OB_LOOKBACK = 20
_OB_MIN_MOVE_PCT = 1.5
_OB_VOL_MULTIPLIER = 1.2

def _detect_order_blocks(df, smc_structure):
    if len(df) < 15: return "No", "No", "—", "—"
    d = df.reset_index(drop=True)
    lookback = min(_OB_LOOKBACK, len(d)-3)
    recent = d.tail(lookback+2).reset_index(drop=True)
    vol_avg = d["Volume"].tail(20).mean()
    last_close = float(d["Close"].iloc[-1])
    bullish_label, bearish_label = "No", "No"
    ob_zone, ob_strength = "—", "—"
    is_bos_bullish = smc_structure in ("BOS 📈","CHOCH 🐂")
    is_bos_bearish = smc_structure in ("BOS 📉","CHOCH 🐻")
    def _strength(move_pct, candle_vol):
        if move_pct >= 4 and vol_avg > 0 and candle_vol >= vol_avg*2: return "Strong"
        if move_pct >= 2.5 or (vol_avg > 0 and candle_vol >= vol_avg*1.5): return "Medium"
        return "Weak"
    try:
        if is_bos_bullish:
            for i in range(len(recent)-2, 0, -1):
                candle = recent.iloc[i]
                if not (candle["Close"] < candle["Open"]): continue
                if i+1 >= len(recent): continue
                move_after = recent["Close"].iloc[i+1:].max()
                move_pct = ((move_after - candle["Close"])/candle["Close"]*100) if candle["Close"] else 0
                vol_ok = vol_avg > 0 and candle["Volume"] >= vol_avg*_OB_VOL_MULTIPLIER
                if move_pct >= _OB_MIN_MOVE_PCT and vol_ok:
                    zone_low, zone_high = round(float(candle["Low"]),2), round(float(candle["High"]),2)
                    if zone_low <= last_close <= zone_high*1.02:
                        bullish_label = "🟢 Bullish OB"; ob_zone = f"{zone_low}–{zone_high}"; ob_strength = _strength(move_pct, float(candle["Volume"]))
                    break
        if is_bos_bearish and bullish_label == "No":
            for i in range(len(recent)-2, 0, -1):
                candle = recent.iloc[i]
                if not (candle["Close"] > candle["Open"]): continue
                if i+1 >= len(recent): continue
                move_after = recent["Close"].iloc[i+1:].min()
                move_pct = ((candle["Close"] - move_after)/candle["Close"]*100) if candle["Close"] else 0
                vol_ok = vol_avg > 0 and candle["Volume"] >= vol_avg*_OB_VOL_MULTIPLIER
                if move_pct >= _OB_MIN_MOVE_PCT and vol_ok:
                    zone_low, zone_high = round(float(candle["Low"]),2), round(float(candle["High"]),2)
                    if zone_low*0.98 <= last_close <= zone_high:
                        bearish_label = "🔴 Bearish OB"; ob_zone = f"{zone_low}–{zone_high}"; ob_strength = _strength(move_pct, float(candle["Volume"]))
                    break
    except (KeyError,IndexError,TypeError,ValueError,ZeroDivisionError,AttributeError):
        return "No","No","—","—"
    return bullish_label, bearish_label, ob_zone, ob_strength

def _parse_ob_zone(ob_zone):
    if not ob_zone or ob_zone == "—": return None, None
    try:
        low_str, high_str = ob_zone.split("–")
        return float(low_str), float(high_str)
    except (ValueError, AttributeError):
        return None, None

# ══════════════════════════════════════════════════════════════════════════
# ── NEW: ENHANCED SIGNAL VALIDATION ENGINE (purely additive) ─────────────
# ══════════════════════════════════════════════════════════════════════════

def calculate_adx(df, period=14):
    """ADX. Returns (ADX, +DI, -DI). ADX>25=trending, <20=sideways."""
    h, l, c = df["High"], df["Low"], df["Close"]
    up_move = h.diff(); down_move = -l.diff()
    plus_dm = pd.Series(np.where((up_move>down_move)&(up_move>0), up_move, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down_move>up_move)&(down_move>0), down_move, 0.0), index=df.index)
    atr_s = calculate_atr(df, period)
    plus_di = 100*(plus_dm.ewm(alpha=1/period,adjust=False).mean()/atr_s.replace(0,np.nan)).fillna(0)
    minus_di = 100*(minus_dm.ewm(alpha=1/period,adjust=False).mean()/atr_s.replace(0,np.nan)).fillna(0)
    dx = (100*(plus_di-minus_di).abs()/(plus_di+minus_di).replace(0,np.nan)).fillna(0)
    adx = dx.ewm(alpha=1/period,adjust=False).mean()
    return round(float(adx.iloc[-1]),1), round(float(plus_di.iloc[-1]),1), round(float(minus_di.iloc[-1]),1)

def detect_fvg(df):
    """Detect most recent Fair Value Gap."""
    if len(df) < 5:
        return {"label":"No FVG","type":None,"gap_size":0.0,"filled_pct":0.0,"age_candles":None,"freshness":"—","mitigated":False,"nearest_dist":None}
    last_close = float(df["Close"].iloc[-1])
    recent = df.tail(30).reset_index(drop=True)
    n = len(recent); best = None
    for i in range(1, n-1):
        prev_high = float(recent["High"].iloc[i-1]); prev_low = float(recent["Low"].iloc[i-1])
        next_high = float(recent["High"].iloc[i+1]); next_low = float(recent["Low"].iloc[i+1])
        bullish = prev_high < next_low; bearish = prev_low > next_high
        if not bullish and not bearish: continue
        if bullish: gap_low, gap_high, direction = prev_high, next_low, "Bullish"
        else: gap_low, gap_high, direction = next_high, prev_low, "Bearish"
        gap_size = round(gap_high - gap_low, 4)
        age_candles = n - 1 - i
        fill_depth = max(0.0, last_close-gap_low) if bullish else max(0.0, gap_high-last_close)
        filled_pct = round(min(fill_depth/gap_size*100, 100), 1) if gap_size > 0 else 100.0
        mitigated = filled_pct >= 50.0
        freshness = "Old" if age_candles > 10 else "Fresh"
        if best is None or age_candles < best["age_candles"]:
            best = {"type":direction,"gap_low":round(gap_low,2),"gap_high":round(gap_high,2),"gap_size":round(gap_size,2),"filled_pct":filled_pct,"age_candles":age_candles,"freshness":freshness,"mitigated":mitigated,"nearest_dist":round(abs(last_close-(gap_low if bullish else gap_high)),2)}
    if best is None:
        return {"label":"No FVG","type":None,"gap_size":0.0,"filled_pct":0.0,"age_candles":None,"freshness":"—","mitigated":False,"nearest_dist":None}
    best["label"] = f"{best['type']} {'Fresh' if not best['mitigated'] else 'Filled'}"
    return best

def classify_order_block(df, smc_structure, fvg):
    """Enhanced OB: Fresh/Mitigated/Institutional/Retail classification."""
    bullish_ob, bearish_ob, ob_zone, ob_strength = _detect_order_blocks(df, smc_structure)
    last_close = float(df["Close"].iloc[-1])
    def _ob_meta(ob_label, side):
        if ob_label == "No": return {"label":f"No {side} OB","fresh":False,"institutional":False,"ob_type":"None"}
        zone_low, zone_high = _parse_ob_zone(ob_zone)
        if zone_low is None: return {"label":ob_label,"fresh":False,"institutional":False,"ob_type":"Unknown"}
        fresh = not (zone_low <= last_close <= zone_high)
        institutional = ob_strength=="Strong" and fvg.get("type")==side and not fvg.get("mitigated",True)
        retail = ob_strength=="Weak" or fvg.get("type")!=side
        if institutional: ob_type, prefix = "Institutional", "Institutional"
        elif retail: ob_type, prefix = "Retail", "Weak"
        else: ob_type, prefix = "Strong", "Strong"
        return {"label":f"{'Fresh' if fresh else 'Mitigated'} {prefix} {side} OB","fresh":fresh,"institutional":institutional,"ob_type":ob_type}
    bull_meta = _ob_meta(bullish_ob, "Bullish")
    bear_meta = _ob_meta(bearish_ob, "Bearish")
    return {"bullish_ob_label":bull_meta["label"],"bearish_ob_label":bear_meta["label"],"ob_zone":ob_zone,"ob_strength":ob_strength,"bull_ob_fresh":bull_meta["fresh"],"bear_ob_fresh":bear_meta["fresh"],"bull_ob_institutional":bull_meta["institutional"],"bear_ob_institutional":bear_meta["institutional"],"bear_ob_type":bear_meta["ob_type"],"bull_ob_type":bull_meta["ob_type"]}

def detect_liquidity_sweep(df):
    """Sell-Side or Buy-Side liquidity sweep detection."""
    if len(df) < 20: return "No Sweep", "None"
    recent = df.tail(20).reset_index(drop=True)
    swing_low = float(recent["Low"].iloc[:-3].min())
    swing_high = float(recent["High"].iloc[:-3].max())
    last_low = float(recent["Low"].iloc[-1]); last_close = float(recent["Close"].iloc[-1]); last_high = float(recent["High"].iloc[-1])
    if last_low < swing_low and last_close > swing_low: return "🔽 Sell-Side Sweep (Buy Setup)", "Buy"
    if last_high > swing_high and last_close < swing_high: return "🔼 Buy-Side Sweep (Sell Setup)", "Sell"
    return "No Sweep", "None"

def detect_htf_trend(df):
    """HTF trend via monthly resampled daily candles."""
    if len(df) < 60: return "Insufficient Data"
    d = df.set_index("Time")
    monthly = d["Close"].resample("ME").last().dropna()
    if len(monthly) < 4: return "N/A"
    span = min(6, len(monthly)-1)
    ema = monthly.ewm(span=span, adjust=False).mean()
    if monthly.iloc[-1] > ema.iloc[-1] and monthly.iloc[-1] > monthly.iloc[-2]: return "🟢 HTF Bullish"
    if monthly.iloc[-1] < ema.iloc[-1] and monthly.iloc[-1] < monthly.iloc[-2]: return "🔴 HTF Bearish"
    return "🟡 HTF Sideways"

def calculate_momentum(df, rsi_val, macd_bullish, adx_val):
    if len(df) < 10: return "⚪ Weak"
    roc5 = (float(df["Close"].iloc[-1])/float(df["Close"].iloc[-5])-1)*100 if len(df)>=5 else 0
    bull = sum([roc5>1.0, rsi_val>55, macd_bullish, adx_val>25])
    bear = sum([roc5<-1.0, rsi_val<45, not macd_bullish, adx_val>25])
    if bull>=3: return "🟢 Strong Bullish"
    if bear>=3: return "🔴 Strong Bearish"
    if bull==2: return "🟡 Moderate Bullish"
    if bear==2: return "🟡 Moderate Bearish"
    return "⚪ Weak"

def _run_20_point_validation(*, htf_trend, smc_structure, cisd_signal, ob_meta, fvg, liquidity_sweep, volume_ok, last_close, prev_close, atr_val, vwap_val, ema20, ema50, rsi_val, macd_bullish, adx_val, rr, direction):
    is_buy = direction == "BUY"
    checks = [
        ("1. HTF Trend",       ("Bullish" in htf_trend) if is_buy else ("Bearish" in htf_trend)),
        ("2. Market Structure", smc_structure in ("BOS 📈","CHOCH 🐂") if is_buy else smc_structure in ("BOS 📉","CHOCH 🐻")),
        ("3. BOS",             "BOS" in smc_structure and ("📈" in smc_structure if is_buy else "📉" in smc_structure)),
        ("4. CHOCH",           "CHOCH" in smc_structure),
        ("5. CISD",            ("Bullish" in cisd_signal) if is_buy else ("Bearish" in cisd_signal)),
        ("6. OB Quality",      ob_meta.get("bull_ob_type") not in ("Retail","None") if is_buy else ob_meta.get("bear_ob_type") not in ("Retail","None")),
        ("7. Fresh OB",        ob_meta.get("bull_ob_fresh",False) if is_buy else ob_meta.get("bear_ob_fresh",False)),
        ("8. Untested OB",     ob_meta.get("bull_ob_fresh",False) if is_buy else ob_meta.get("bear_ob_fresh",False)),
        ("9. Liquidity Sweep", ("Buy" in liquidity_sweep) if is_buy else ("Sell" in liquidity_sweep)),
        ("10. FVG",            fvg.get("type")==("Bullish" if is_buy else "Bearish") and not fvg.get("mitigated",True)),
        ("11. Volume",         volume_ok),
        ("12. Candle",         (last_close>prev_close) if is_buy else (last_close<prev_close)),
        ("13. ATR Move",       abs(last_close-prev_close)>=0.3*atr_val if atr_val and atr_val>0 else False),
        ("14. Momentum",       rsi_val>45 if is_buy else rsi_val<55),
        ("15. VWAP",           (last_close>vwap_val) if (is_buy and vwap_val) else ((last_close<vwap_val) if vwap_val else False)),
        ("16. EMA Trend",      (ema20>ema50) if is_buy else (ema20<ema50)),
        ("17. RSI",            (45<rsi_val<80) if is_buy else (20<rsi_val<55)),
        ("18. MACD",           macd_bullish if is_buy else not macd_bullish),
        ("19. ADX > 20",       adx_val >= 20),
        ("20. RR >= 1:2",      rr >= 2.0),
    ]
    passed = [name for name,ok in checks if ok]
    failed = [name for name,ok in checks if not ok]
    return len(passed), passed, failed

def _signal_grade(passed_count, ai_confidence):
    if passed_count == 20 and ai_confidence >= 93: return "A+"
    if passed_count >= 17 and ai_confidence >= 88: return "A"
    if passed_count >= 14 and ai_confidence >= 80: return "B"
    if passed_count >= 10 and ai_confidence >= 70: return "C"
    return "REJECT"

def _enhanced_ai_confidence(passed_count, adx_val, rvol_raw, fvg, liquidity_swept, htf_aligned, ob_institutional):
    base = (passed_count/20)*70
    bonus = min(adx_val/50*8, 8) + min((rvol_raw-1)*3, 6)
    bonus += 4 if not fvg.get("mitigated",True) else 0
    bonus += 4 if liquidity_swept else 0
    bonus += 4 if htf_aligned else 0
    bonus += 4 if ob_institutional else 0
    return round(max(0.0, min(100.0, base+bonus)), 1)

def _build_reason_list(direction, passed_list, fvg, liquidity_sweep, htf_trend, ob_meta):
    is_buy = direction == "BUY"; reasons = []
    if "Bullish" in htf_trend or "Bearish" in htf_trend: reasons.append(f"HTF {'Uptrend ✓' if is_buy else 'Downtrend ✓'}")
    if any("BOS" in p for p in passed_list): reasons.append(f"{'Bullish' if is_buy else 'Bearish'} BOS Confirmed ✓")
    if any("CHOCH" in p for p in passed_list): reasons.append("CHOCH Confirmed ✓")
    if any("CISD" in p for p in passed_list): reasons.append(f"{'Bullish' if is_buy else 'Bearish'} CISD ✓")
    ob_label = ob_meta.get("bullish_ob_label" if is_buy else "bearish_ob_label", "")
    if "Fresh" in ob_label or "Institutional" in ob_label: reasons.append("Fresh Demand OB ✓" if is_buy else "Fresh Supply OB ✓")
    if fvg.get("type") and not fvg.get("mitigated"): reasons.append(f"{'Bullish' if is_buy else 'Bearish'} FVG (Unmitigated) ✓")
    if "Sweep" in liquidity_sweep: reasons.append(f"Liquidity Sweep ✓ {liquidity_sweep}")
    if any("Volume" in p for p in passed_list): reasons.append("Volume Expansion ✓")
    if any("VWAP" in p for p in passed_list): reasons.append(f"{'Above' if is_buy else 'Below'} VWAP ✓")
    if any("EMA" in p for p in passed_list): reasons.append("EMA Alignment ✓")
    if any("MACD" in p for p in passed_list): reasons.append(f"MACD {'Bullish' if is_buy else 'Bearish'} ✓")
    if any("RSI" in p for p in passed_list): reasons.append(f"RSI {'Bullish Zone' if is_buy else 'Bearish Zone'} ✓")
    if any("Momentum" in p for p in passed_list): reasons.append("Strong Momentum ✓")
    if any("RR" in p for p in passed_list): reasons.append("Risk:Reward ≥ 1:2 ✓")
    if ob_meta.get("bull_ob_institutional" if is_buy else "bear_ob_institutional", False): reasons.append("Institutional Buying ✓" if is_buy else "Institutional Selling ✓")
    return reasons

def _build_ai_report(direction, htf_trend, smc_structure, ob_meta, fvg, liquidity_sweep, volume_ok, vwap_val, last_close, ema20, ema50, momentum, adx_val, atr_val, ai_confidence, grade):
    is_buy = direction == "BUY"
    lines = [
        f"HTF Trend     : {htf_trend}",
        f"Structure     : {smc_structure}",
        f"OB            : {ob_meta.get('bullish_ob_label' if is_buy else 'bearish_ob_label','—')}",
        f"FVG           : {fvg.get('label','No FVG')}",
        f"Liquidity     : {liquidity_sweep}",
        f"Volume        : {'High ✓' if volume_ok else 'Low ✗'}",
        f"VWAP          : {'Above ✓' if vwap_val and last_close>vwap_val else 'Below'}",
        f"EMA Trend     : {'EMA20>EMA50 ✓' if ema20>ema50 else 'EMA20<EMA50'}",
        f"Momentum      : {momentum}",
        f"ADX           : {adx_val} ({'Trending ✓' if adx_val>=25 else 'Sideways'})",
        f"ATR           : {round(atr_val,2) if atr_val else '—'}",
        f"Risk          : {'Low' if grade in ('A+','A') else 'Medium' if grade=='B' else 'High'}",
        f"Confidence    : {ai_confidence}%",
        f"Trade Quality : {'Institutional ✓' if ob_meta.get('bull_ob_institutional' if is_buy else 'bear_ob_institutional') else 'Retail'}",
    ]
    return " | ".join(lines)

ENHANCED_MIN_CONFIDENCE = 80.0
_PASSING_GRADES = {"A+","A","B","C"}

def _analyse_enhanced(symbol, df, nifty_close, enable_xgboost):
    """Calls existing _analyse() then appends all new enhanced columns."""
    base = _analyse(symbol, df, nifty_close, enable_xgboost)
    close = df["Close"]
    last_close = float(close.iloc[-1])
    prev_close = float(close.iloc[-2]) if len(close)>=2 else last_close
    ema20 = float(close.ewm(span=20).mean().iloc[-1])
    ema50 = float(close.ewm(span=50).mean().iloc[-1])
    rsi_val = base["RSI"]
    macd_bullish = "Bullish" in base["MACD Signal"]
    vwap_val = base["VWAP"]
    atr_val = base.get("_ATR14") or last_close*0.01
    rvol_raw = base.get("_RVOL_RAW", 0.0)
    vol_avg20 = float(df["Volume"].tail(20).mean())
    volume_ok = bool(vol_avg20>0 and float(df["Volume"].iloc[-1])>vol_avg20)
    adx_val, plus_di, minus_di = calculate_adx(df)
    fvg = detect_fvg(df)
    liquidity_sweep, sweep_side = detect_liquidity_sweep(df)
    htf_trend = detect_htf_trend(df)
    momentum = calculate_momentum(df, rsi_val, macd_bullish, adx_val)
    smc_structure = base["SMC Structure"]; cisd_signal = base["CISD"]
    ob_meta = classify_order_block(df, smc_structure, fvg)
    quality_direction = "BUY" if base.get("AI Score", 50) >= 55 else "SELL"
    if quality_direction == "BUY":
        sl = round(last_close-1.5*atr_val,2); t1 = round(last_close+2.0*atr_val,2)
        t2 = round(last_close+3.5*atr_val,2); t3 = round(last_close+5.0*atr_val,2)
    else:
        sl = round(last_close+1.5*atr_val,2); t1 = round(last_close-2.0*atr_val,2)
        t2 = round(last_close-3.5*atr_val,2); t3 = round(last_close-5.0*atr_val,2)
    risk_amt = abs(last_close-sl); reward_amt = abs(t1-last_close)
    rr = round(reward_amt/risk_amt,2) if risk_amt>0 else 0.0
    passed_count, passed_list, failed_list = _run_20_point_validation(
        htf_trend=htf_trend, smc_structure=smc_structure, cisd_signal=cisd_signal, ob_meta=ob_meta,
        fvg=fvg, liquidity_sweep=liquidity_sweep, volume_ok=volume_ok, last_close=last_close,
        prev_close=prev_close, atr_val=atr_val, vwap_val=vwap_val, ema20=ema20, ema50=ema50,
        rsi_val=rsi_val, macd_bullish=macd_bullish, adx_val=adx_val, rr=rr, direction=quality_direction)
    htf_aligned = ("Bullish" in htf_trend and quality_direction=="BUY") or ("Bearish" in htf_trend and quality_direction=="SELL")
    ob_institutional = ob_meta.get("bull_ob_institutional",False) if quality_direction=="BUY" else ob_meta.get("bear_ob_institutional",False)
    liquidity_swept = "Sweep" in liquidity_sweep
    ai_confidence = _enhanced_ai_confidence(passed_count, adx_val, rvol_raw, fvg, liquidity_swept, htf_aligned, ob_institutional)
    grade = _signal_grade(passed_count, ai_confidence)
    reasons = _build_reason_list(quality_direction, passed_list, fvg, liquidity_sweep, htf_trend, ob_meta)
    reason_str = " | ".join(reasons) if reasons else "—"
    ai_report = _build_ai_report(quality_direction, htf_trend, smc_structure, ob_meta, fvg, liquidity_sweep, volume_ok, vwap_val, last_close, ema20, ema50, momentum, adx_val, atr_val, ai_confidence, grade)
    if grade=="REJECT" or ai_confidence<ENHANCED_MIN_CONFIDENCE: enhanced_decision="⬛ REJECT"; enhanced_signal="⬛ Rejected"
    elif grade=="A+" and quality_direction=="BUY": enhanced_decision="🟢🟢 Institutional BUY"; enhanced_signal="🟢🟢 Strong BUY"
    elif grade=="A+" and quality_direction=="SELL": enhanced_decision="🔴🔴 Institutional SELL"; enhanced_signal="🔴🔴 Strong SELL"
    elif grade=="A" and quality_direction=="BUY": enhanced_decision="🟢 BUY"; enhanced_signal="🟢 BUY"
    elif grade=="A" and quality_direction=="SELL": enhanced_decision="🔴 SELL"; enhanced_signal="🔴 SELL"
    elif grade=="B" and quality_direction=="BUY": enhanced_decision="🟢 BUY (B)"; enhanced_signal="🟢 BUY"
    elif grade=="B" and quality_direction=="SELL": enhanced_decision="🔴 SELL (B)"; enhanced_signal="🔴 SELL"
    else: enhanced_decision="🟠 WAIT"; enhanced_signal="🟠 WAIT"
    enhanced_cols = {
        "HTF Trend":htf_trend,"ADX":adx_val,"+DI":plus_di,"-DI":minus_di,"Momentum":momentum,
        "FVG":fvg.get("label","No FVG"),"FVG Gap Size":fvg.get("gap_size",0.0),
        "FVG Filled %":fvg.get("filled_pct",0.0),"FVG Age (Candles)":fvg.get("age_candles"),
        "FVG Freshness":fvg.get("freshness","—"),"FVG Mitigated":"Yes" if fvg.get("mitigated") else "No",
        "FVG Nearest Distance":fvg.get("nearest_dist"),
        "OB Type (Bullish)":ob_meta.get("bullish_ob_label","No Bullish OB"),
        "OB Type (Bearish)":ob_meta.get("bearish_ob_label","No Bearish OB"),
        "Liquidity Sweep":liquidity_sweep,"Confirmations Passed":passed_count,
        "Confirmations Failed":", ".join(failed_list) if failed_list else "None",
        "Signal Grade":grade,"AI Confidence %":ai_confidence,
        "Enhanced Entry":round(last_close,2),"Enhanced SL":sl,
        "Enhanced Target 1":t1,"Enhanced Target 2":t2,"Enhanced Target 3":t3,"Enhanced RR":rr,
        "Signal Reason":reason_str,"AI Report":ai_report,
        "Enhanced Signal":enhanced_signal,"Enhanced Decision":enhanced_decision,
        "_Enhanced_Pass":grade in _PASSING_GRADES and ai_confidence>=ENHANCED_MIN_CONFIDENCE,
    }
    return {**base, **enhanced_cols}

def _fetch_symbol_enhanced(fyers, symbol, nifty_close, enable_xgboost):
    if not isinstance(symbol,str) or not _VALID_EQ_SYMBOL_RE.match(symbol):
        return None, f"{symbol}: invalid symbol format — skipped"
    resp, err = _safe_history(fyers, {"symbol":symbol,"resolution":"D","date_format":"1","range_from":DATE_FROM,"range_to":DATE_TO,"cont_flag":"1"})
    if err: return None, f"{symbol}: {err}"
    candles = resp.get("candles") if resp else None
    if not candles or len(candles)<30: return None, f"{symbol}: insufficient history"
    try:
        df = pd.DataFrame(candles, columns=["Time","Open","High","Low","Close","Volume"])
        df["Time"] = pd.to_datetime(df["Time"],unit="s",utc=True).dt.tz_convert("Asia/Kolkata")
        df[["Open","High","Low","Close","Volume"]] = df[["Open","High","Low","Close","Volume"]].apply(pd.to_numeric,errors="coerce")
        df = df.dropna(subset=["Open","High","Low","Close"])
        if len(df)<30: return None, f"{symbol}: insufficient valid candle data"
    except (KeyError,ValueError,TypeError) as e:
        return None, f"{symbol}: malformed candle data ({e})"
    try:
        return _analyse_enhanced(symbol, df, nifty_close, enable_xgboost), None
    except Exception as e:
        return None, f"{symbol}: enhanced analysis error ({type(e).__name__})"

def run_scan_enhanced(fyers, symbols, nifty_close, enable_xgboost):
    symbols = _validate_symbols(symbols)
    results, errors = [], []
    stats = ScanStats(total=len(symbols))
    progress = st.progress(0.0, text=f"Institutional Scan 0 / {len(symbols)}")
    done = 0
    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i:i+BATCH_SIZE]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(_fetch_symbol_enhanced, fyers, s, nifty_close, enable_xgboost): s for s in batch}
            for future in as_completed(futures):
                try: res, err = future.result()
                except Exception as e: res, err = None, f"{futures[future]}: worker error ({type(e).__name__})"
                if res: results.append(res)
                if err: errors.append(err)
                stats.record(has_result=bool(res), has_error=bool(err))
                done += 1
                progress.progress(done/max(len(symbols),1), text=f"Institutional Scan {done} / {len(symbols)}")
        if i+BATCH_SIZE < len(symbols): time.sleep(BATCH_PAUSE_SECONDS)
    progress.empty()
    return results, errors, stats

def _analyse(symbol, df, nifty_close, enable_xgboost):
    close, volume = df["Close"], df["Volume"]
    ema20 = close.ewm(span=20).mean().iloc[-1]
    ema50 = close.ewm(span=50).mean().iloc[-1]
    ema200 = close.ewm(span=200).mean().iloc[-1] if len(close)>=200 else close.ewm(span=len(close)).mean().iloc[-1]
    vol_avg20 = volume.tail(20).mean()
    rvol = (volume.iloc[-1]/vol_avg20) if vol_avg20>0 else 0
    trend_score = sum([close.iloc[-1]>ema20, close.iloc[-1]>ema50, close.iloc[-1]>ema200])/3
    roc = (close.iloc[-1]/close.iloc[-10]-1)*100 if len(close)>=10 else 0
    ai_score = min(round((rvol*15)+(trend_score*40)+min(max(roc,0),10)*2+20,1),100)
    gap_pct = 0.0
    if len(df)>=2 and df["Close"].iloc[-2] not in (0,None) and pd.notna(df["Close"].iloc[-2]):
        gap_pct = ((df["Open"].iloc[-1]-df["Close"].iloc[-2])/df["Close"].iloc[-2])*100
    gap_str = f"{gap_pct:.2f}%"
    if gap_pct>=0.5: gap_str+=" 🟢"
    elif gap_pct<=-0.5: gap_str+=" 🔴"
    smc_structure, cisd_signal, _signal_event_ts = _calculate_smc_and_cisd(df)
    bullish_ob, bearish_ob, ob_zone, ob_strength = _detect_order_blocks(df, smc_structure)
    h52w = df["High"].max(); l52w = df["Low"].min(); last_close = close.iloc[-1]
    if pd.notna(h52w) and last_close>=h52w*0.97: status_52w="🟢 Near High"
    elif pd.notna(l52w) and last_close<=l52w*1.03: status_52w="🔴 Near Low"
    else: status_52w="Mid Range"
    resistance = df["High"].rolling(20).max().shift(1).iloc[-1]
    support = df["Low"].rolling(20).min().shift(1).iloc[-1]
    if pd.notna(resistance) and last_close>resistance: breakout="📈 Bullish"
    elif pd.notna(support) and last_close<support: breakout="📉 Bearish"
    else: breakout="NO"
    rsi_val = round(float(calculate_rsi(close).iloc[-1]),1)
    macd_line, signal_line, macd_hist = calculate_macd(close)
    macd_bullish = bool(macd_line.iloc[-1]>signal_line.iloc[-1])
    macd_signal_str = "🟢 Bullish Crossover" if macd_bullish else "🔴 Bearish Crossover"
    supertrend_label, supertrend_bullish, _ = calculate_supertrend(df)
    vwap_val = calculate_vwap_approx(df)
    chart_pattern = detect_chart_pattern(df)
    mtf_trend = calculate_mtf_trend(df)
    rs_label = calculate_relative_strength(close, nifty_close)
    atr14 = calculate_atr(df).iloc[-1]
    direction = "Bullish" if breakout=="📈 Bullish" or macd_bullish else ("Bearish" if breakout=="📉 Bearish" or not macd_bullish else "Neutral")
    target, stoploss = calculate_target_stoploss(last_close, atr14, direction)
    ai_trend, ai_confidence = calculate_ai_trend(ai_score)
    xgb_trend, xgb_confidence = calculate_xgboost_prediction(df, rsi_val=rsi_val, macd_bullish=macd_bullish, supertrend_bullish=supertrend_bullish, vwap_val=vwap_val, rvol=rvol, support=support, resistance=resistance, use_ml=enable_xgboost)
    alerts = generate_alerts(rvol, breakout, cisd_signal, mtf_trend, gap_pct)
    final_signal = calculate_final_signal(ai_score=ai_score, xgb_trend=xgb_trend, mtf_trend=mtf_trend, rs_label=rs_label, rsi=rsi_val, macd_bullish=macd_bullish, supertrend_bullish=supertrend_bullish, breakout=breakout, cisd_signal=cisd_signal, smc_structure=smc_structure)
    stock_ticker = symbol.replace("NSE:","").replace("-EQ","")
    news = calculate_news(stock_ticker, gap_pct, rvol, breakout)
    rvol_raw = round(float(rvol),2)
    rvol_display = _format_rvol_display(rvol_raw)
    quality_direction, quality_count, is_high_quality, signal_strength, signal_reason = _calculate_signal_quality(ema20=float(ema20), ema50=float(ema50), rsi_val=rsi_val, macd_bullish=macd_bullish, supertrend_bullish=supertrend_bullish, vwap_val=vwap_val, last_close=float(last_close), rvol_raw=rvol_raw, breakout=breakout, cisd_signal=cisd_signal, smc_structure=smc_structure, last_volume=float(volume.iloc[-1]), vol_avg20=float(vol_avg20))
    entry_confirmation, trade_quality, trade_decision = _determine_entry_and_decision(direction=quality_direction, confirmed_count=quality_count, ai_score=ai_score, confidence=xgb_confidence, rvol_raw=rvol_raw, volume_ok=bool(vol_avg20 and vol_avg20>0 and float(volume.iloc[-1])>vol_avg20))
    if _signal_event_ts is not None: signal_date_str, signal_time_str = _format_signal_timestamp(_signal_event_ts, is_daily=True)
    else: signal_date_str, signal_time_str = _candle_signal_timestamp(df, is_daily=True)
    return {
        "Signal Date":signal_date_str,"Signal Time":signal_time_str,"Stock":stock_ticker,"LTP":round(last_close,2),"Gap %":gap_str,"Target":target,"Stoploss":stoploss,
        "SMC Structure":smc_structure,"CISD":cisd_signal,"Bullish Order Block":bullish_ob,"Bearish Order Block":bearish_ob,"Order Block Zone":ob_zone,"Order Block Strength":ob_strength,
        "XGBoost Trend":xgb_trend,"XGBoost Confidence (%)":xgb_confidence,"News":news,"Alerts":alerts,"Signal Strength":signal_strength,"Entry Confirmation":entry_confirmation,
        "Signal Reason":signal_reason,"Trade Quality":trade_quality,"Trade Decision":trade_decision,"MTF Trend":mtf_trend,"AI Trend":ai_trend,"AI Confidence (%)":ai_confidence,
        "RS vs NIFTY":rs_label,"Support":round(float(support),2) if pd.notna(support) else None,"Resistance":round(float(resistance),2) if pd.notna(resistance) else None,
        "52W High":round(float(h52w),2) if pd.notna(h52w) else None,"52W Low":round(float(l52w),2) if pd.notna(l52w) else None,"52W Status":status_52w,
        "RSI":rsi_val,"Breakout Status":breakout,"MACD Signal":macd_signal_str,"Supertrend":supertrend_label,"VWAP":vwap_val,"Chart Pattern":chart_pattern,"RVOL":rvol_display,
        "AI Score":ai_score,"Final Signal":final_signal,"Smart Money":"🏦 Institutional" if ai_score>70 else "⚖️ Neutral" if ai_score>45 else "🔻 Distribution",
        "Signal":"🟢 BUY" if ai_score>65 else "🔴 SELL" if ai_score<40 else "🟡 HOLD",
        "_ATR14":round(float(atr14),2) if pd.notna(atr14) else round(last_close*0.01,2),"_RVOL_RAW":rvol_raw,"_Is_High_Quality":is_high_quality,"_Quality_Count":quality_count,
    }

def calculate_intraday_signal(row):
    try:
        last_close=row["LTP"]; atr=row.get("_ATR14") or round(last_close*0.01,2)
        rsi=row["RSI"]; macd_bullish="Bullish" in row["MACD Signal"]
        supertrend_label=row["Supertrend"]; vwap=row["VWAP"]
        rvol=row.get("_RVOL_RAW",0.0); breakout=row["Breakout Status"]; ai_score=row["AI Score"]
        bull_votes=sum([macd_bullish,"Buy" in supertrend_label,vwap is not None and last_close>vwap,rsi>50,breakout=="📈 Bullish"])
        bear_votes=sum([not macd_bullish,"Sell" in supertrend_label,vwap is not None and last_close<vwap,rsi<50,breakout=="📉 Bearish"])
        if bull_votes>=4 and rvol>=1.2: signal="🟢 BUY"
        elif bear_votes>=4 and rvol>=1.2: signal="🔴 SELL"
        else: signal="🟡 WAIT"
        entry=round(last_close,2)
        if signal=="🟢 BUY": sl=round(entry-1.0*atr,2);t1=round(entry+1.0*atr,2);t2=round(entry+1.8*atr,2);t3=round(entry+2.6*atr,2);exit_cond="Exit if price closes below SL or Supertrend flips Sell"
        elif signal=="🔴 SELL": sl=round(entry+1.0*atr,2);t1=round(entry-1.0*atr,2);t2=round(entry-1.8*atr,2);t3=round(entry-2.6*atr,2);exit_cond="Exit if price closes above SL or Supertrend flips Buy"
        else: sl=round(entry-1.0*atr,2);t1=t2=t3=entry;exit_cond="No trade — wait for alignment"
        risk=abs(entry-sl); reward=abs(t1-entry)
        rr_ratio=round(reward/risk,2) if risk>0 else 0.0
        vote_total=max(bull_votes,bear_votes)
        confidence=max(30.0,round(min(95.0,40+vote_total*11+min(rvol,3)*5),1))
        atr_pct=(atr/last_close*100) if last_close else 0
        if atr_pct>=3: holding_time="15–45 Minutes (high volatility)"
        elif atr_pct>=1.5: holding_time="30–90 Minutes"
        else: holding_time="1–3 Hours"
        reasons=["MACD bullish" if macd_bullish else "MACD bearish", f"Supertrend {supertrend_label.split()[-1]}"]
        if vwap is not None: reasons.append("Above VWAP" if last_close>vwap else "Below VWAP")
        reasons.append(f"RSI {rsi}")
        if rvol>=1.5: reasons.append(f"High RVOL {rvol}x")
        if breakout!="NO": reasons.append(f"Breakout: {breakout}")
        return {"Signal Date":row["Signal Date"],"Signal Time":row["Signal Time"],"Stock":row["Stock"],"LTP":last_close,"Intraday Signal":signal,"Entry Price":entry,"Stop Loss":sl,"Target 1":t1,"Target 2":t2,"Target 3":t3,"Risk Reward Ratio":rr_ratio,"Confidence %":confidence,"AI Score":ai_score,"Bullish Order Block":row.get("Bullish Order Block","No"),"Bearish Order Block":row.get("Bearish Order Block","No"),"Order Block Zone":row.get("Order Block Zone","—"),"Order Block Strength":row.get("Order Block Strength","—"),"Expected Holding Time":holding_time,"Exit Condition":exit_cond,"Reason":", ".join(reasons)}
    except (KeyError,IndexError,TypeError,ValueError,ZeroDivisionError,AttributeError):
        return {"Signal Date":row.get("Signal Date","N/A"),"Signal Time":row.get("Signal Time","N/A"),"Stock":row.get("Stock","N/A"),"LTP":row.get("LTP"),"Intraday Signal":"🟡 WAIT","Entry Price":row.get("LTP"),"Stop Loss":None,"Target 1":None,"Target 2":None,"Target 3":None,"Risk Reward Ratio":0.0,"Confidence %":0.0,"AI Score":row.get("AI Score",0),"Bullish Order Block":row.get("Bullish Order Block","No"),"Bearish Order Block":row.get("Bearish Order Block","No"),"Order Block Zone":row.get("Order Block Zone","—"),"Order Block Strength":row.get("Order Block Strength","—"),"Expected Holding Time":"N/A","Exit Condition":"Insufficient data","Reason":"Insufficient data"}

def calculate_swing_signal(row):
    try:
        last_close=row["LTP"]; atr=row.get("_ATR14") or round(last_close*0.01,2)
        mtf_trend=row["MTF Trend"]; rs_label=row["RS vs NIFTY"]
        supertrend_label=row["Supertrend"]; smc_structure=row["SMC Structure"]
        cisd_signal=row["CISD"]; ai_score=row["AI Score"]
        bull_votes=sum(["Aligned Bullish" in mtf_trend,"Outperform" in rs_label,"Buy" in supertrend_label,"📈" in smc_structure or "🐂" in smc_structure,"Bullish" in cisd_signal])
        bear_votes=sum(["Aligned Bearish" in mtf_trend,"Underperform" in rs_label,"Sell" in supertrend_label,"📉" in smc_structure or "🐻" in smc_structure,"Bearish" in cisd_signal])
        if bull_votes>=3: signal="🟢 BUY"
        elif bear_votes>=3: signal="🔴 SELL"
        else: signal="🟡 HOLD"
        entry=round(last_close,2)
        if signal=="🟢 BUY": sl=round(entry-2.0*atr,2);t1=round(entry+2.0*atr,2);t2=round(entry+3.5*atr,2);t3=round(entry+5.0*atr,2);exit_cond="Exit on daily close below SL or MTF turns Bearish"
        elif signal=="🔴 SELL": sl=round(entry+2.0*atr,2);t1=round(entry-2.0*atr,2);t2=round(entry-3.5*atr,2);t3=round(entry-5.0*atr,2);exit_cond="Exit on daily close above SL or MTF turns Bullish"
        else: sl=round(entry-2.0*atr,2);t1=t2=t3=entry;exit_cond="No position — wait for alignment"
        risk=abs(entry-sl); reward=abs(t1-entry)
        rr_ratio=round(reward/risk,2) if risk>0 else 0.0
        vote_total=max(bull_votes,bear_votes)
        confidence=max(30.0,round(min(95.0,38+vote_total*12+(ai_score-50)*0.15),1))
        trend_strength="🟢 Strong" if vote_total>=4 else ("🟡 Moderate" if vote_total==3 else "🔴 Weak")
        atr_pct=(atr/last_close*100) if last_close else 0
        if atr_pct>=3: holding_days,est_days="3–7 Days",5
        elif atr_pct>=1.5: holding_days,est_days="7–14 Days",10
        else: holding_days,est_days="14–25 Days",18
        exit_date=(_now_ist()+timedelta(days=est_days)).strftime("%d-%b-%Y")
        reasons=[f"MTF: {mtf_trend}",f"RS vs NIFTY: {rs_label}",f"Supertrend: {supertrend_label}",f"SMC: {smc_structure}"]
        if cisd_signal!="None": reasons.append(f"CISD: {cisd_signal}")
        return {"Signal Date":row["Signal Date"],"Signal Time":row["Signal Time"],"Stock":row["Stock"],"Swing Signal":signal,"Swing Entry":entry,"Swing Stop Loss":sl,"Swing Target 1":t1,"Swing Target 2":t2,"Swing Target 3":t3,"Expected Holding Period":holding_days,"Estimated Exit Date":exit_date,"Exit Condition":exit_cond,"Trend Strength":trend_strength,"Confidence %":confidence,"AI Score":ai_score,"Risk Reward Ratio":rr_ratio,"Bullish Order Block":row.get("Bullish Order Block","No"),"Bearish Order Block":row.get("Bearish Order Block","No"),"Order Block Zone":row.get("Order Block Zone","—"),"Order Block Strength":row.get("Order Block Strength","—"),"Reason":", ".join(reasons)}
    except (KeyError,IndexError,TypeError,ValueError,ZeroDivisionError,AttributeError):
        return {"Signal Date":row.get("Signal Date","N/A"),"Signal Time":row.get("Signal Time","N/A"),"Stock":row.get("Stock","N/A"),"Swing Signal":"🟡 HOLD","Swing Entry":row.get("LTP"),"Swing Stop Loss":None,"Swing Target 1":None,"Swing Target 2":None,"Swing Target 3":None,"Expected Holding Period":"N/A","Estimated Exit Date":"N/A","Exit Condition":"Insufficient data","Trend Strength":"🔴 Weak","Confidence %":0.0,"AI Score":row.get("AI Score",0),"Risk Reward Ratio":0.0,"Bullish Order Block":row.get("Bullish Order Block","No"),"Bearish Order Block":row.get("Bearish Order Block","No"),"Order Block Zone":row.get("Order Block Zone","—"),"Order Block Strength":row.get("Order Block Strength","—"),"Reason":"Insufficient data"}

def _fetch_symbol(fyers, symbol, nifty_close, enable_xgboost):
    if not isinstance(symbol,str) or not _VALID_EQ_SYMBOL_RE.match(symbol):
        return None, f"{symbol}: invalid symbol format — skipped"
    resp, err = _safe_history(fyers, {"symbol":symbol,"resolution":"D","date_format":"1","range_from":DATE_FROM,"range_to":DATE_TO,"cont_flag":"1"})
    if err: return None, f"{symbol}: {err}"
    candles = resp.get("candles") if resp else None
    if not candles or len(candles)<30: return None, f"{symbol}: insufficient history ({len(candles) if candles else 0} candles)"
    try:
        df = pd.DataFrame(candles, columns=["Time","Open","High","Low","Close","Volume"])
        df["Time"] = pd.to_datetime(df["Time"],unit="s",utc=True).dt.tz_convert("Asia/Kolkata")
        df[["Open","High","Low","Close","Volume"]] = df[["Open","High","Low","Close","Volume"]].apply(pd.to_numeric,errors="coerce")
        df = df.dropna(subset=["Open","High","Low","Close"])
        if len(df)<30: return None, f"{symbol}: insufficient valid candle data after cleaning"
    except (KeyError,ValueError,TypeError) as e:
        return None, f"{symbol}: malformed candle data ({e})"
    try:
        return _analyse(symbol, df, nifty_close, enable_xgboost), None
    except (KeyError,IndexError,TypeError,ValueError,ZeroDivisionError,AttributeError) as e:
        return None, f"{symbol}: analysis error ({type(e).__name__})"
    except Exception as e:
        return None, f"{symbol}: unexpected error ({type(e).__name__})"

def run_scan(fyers, symbols, nifty_close, enable_xgboost):
    symbols = _validate_symbols(symbols)
    results, errors = [], []
    stats = ScanStats(total=len(symbols))
    progress = st.progress(0.0, text=f"Scanning 0 / {len(symbols)}")
    done = 0
    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i:i+BATCH_SIZE]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(_fetch_symbol, fyers, s, nifty_close, enable_xgboost): s for s in batch}
            for future in as_completed(futures):
                try: res, err = future.result()
                except Exception as e: res, err = None, f"{futures[future]}: worker error ({type(e).__name__})"
                if res: results.append(res)
                if err: errors.append(err)
                stats.record(has_result=bool(res), has_error=bool(err))
                done += 1
                progress.progress(done/len(symbols), text=f"Scanning {done} / {len(symbols)}")
        if i+BATCH_SIZE < len(symbols): time.sleep(BATCH_PAUSE_SECONDS)
    progress.empty()
    return results, errors, stats

def _color_code(val):
    if isinstance(val, str):
        if any(x in val for x in ["Strong Buy","BUY","Institutional","🟢","🔵","Buy","BOS 📈","CHOCH 🐂","Bullish","Aligned Bullish","Outperform","Near High","Bullish Engulfing","Hammer","Higher Highs","📈","Up","Golden Cross"]):
            return "color: green; font-weight: bold;"
        if any(x in val for x in ["Strong Sell","SELL","Sell","Distribution","🔴","🟠","BOS 📉","CHOCH 🐻","Bearish","Aligned Bearish","Underperform","Near Low","Bearish Engulfing","Shooting Star","Lower Highs","📉","Down","Death Cross","REJECT"]):
            return "color: red; font-weight: bold;"
        if any(x in val for x in ["🟡","Wait","HOLD","Neutral","Mixed","Inline","WAIT","WATCH"]):
            return "color: #b8860b; font-weight: bold;"
        if any(x in val for x in ["⬛","Rejected"]):
            return "color: #888888; font-weight: bold;"
    return ""

def _style_dataframe(df):
    """Apply colour coding safely. Falls back to unstyled df on any error."""
    try:
        # Work only on string columns — numeric columns never need colour coding
        # and passing them to _color_code causes applymap/map to break.
        str_cols = [c for c in df.columns if df[c].dtype == object]
        if not str_cols:
            return df.style           # nothing to colour — return plain styler
        styler = df.style
        if hasattr(styler, "map"):
            return styler.map(_color_code, subset=str_cols)
        return styler.applymap(_color_code, subset=str_cols)
    except Exception:
        try:
            return df.style           # plain unstyled styler
        except Exception:
            return df                 # raw DataFrame as last resort

_SIGNAL_FILL_RULES = [
    ("STRONG BUY","006100","FFFFFF",True),("STRONG SELL","9C0006","FFFFFF",True),
    ("WATCHLIST","FFA500","000000",True),("BUY","92D050","000000",True),
    ("SELL","FF0000","FFFFFF",True),("WAIT","FFFF00","000000",True),
    ("HOLD","FFFF00","000000",True),("WATCH","FFFF00","000000",True),
    ("CISD UP","92D050","000000",True),("CISD DOWN","FF0000","FFFFFF",True),
    ("REJECT","888888","FFFFFF",True),
]
_SUPPORT_FILL_HEX="E2EFDA"; _RESISTANCE_FILL_HEX="FCE4D6"
_HIGH_AI_SCORE_FILL_HEX="7030A0"; _HIGH_RVOL_FILL_HEX="00FFFF"
_HEADER_FILL_HEX="1F4E78"; _BAND_FILL_HEX="F2F2F2"

def _get_conditional_fill_font(col_name, value):
    from openpyxl.styles import Font, PatternFill
    text = "" if value is None else str(value)
    text_upper = text.upper()
    for keyword, fill_hex, font_hex, bold in _SIGNAL_FILL_RULES:
        if keyword in text_upper:
            return PatternFill("solid", fgColor=fill_hex), Font(color=font_hex, bold=bold)
    if col_name=="Support": return PatternFill("solid",fgColor=_SUPPORT_FILL_HEX), None
    if col_name=="Resistance": return PatternFill("solid",fgColor=_RESISTANCE_FILL_HEX), None
    if "RVOL" in col_name and ("❤️" in text or "🔥" in text): return PatternFill("solid",fgColor=_HIGH_RVOL_FILL_HEX), Font(bold=True)
    if col_name=="AI Score":
        try:
            if float(value)>90: return PatternFill("solid",fgColor=_HIGH_AI_SCORE_FILL_HEX), Font(color="FFFFFF",bold=True)
        except (TypeError,ValueError): pass
    return None, None

def _format_worksheet(ws, df):
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    thin=Side(style="thin",color="B0B0B0"); border=Border(left=thin,right=thin,top=thin,bottom=thin)
    center=Alignment(horizontal="center",vertical="center",wrap_text=False)
    header_font=Font(bold=True,color="FFFFFF",name="Arial",size=11)
    header_fill=PatternFill("solid",fgColor=_HEADER_FILL_HEX)
    for cell in ws[1]:
        cell.font=header_font; cell.fill=header_fill; cell.alignment=center; cell.border=border
    columns=list(df.columns); band_fill=PatternFill("solid",fgColor=_BAND_FILL_HEX)
    for r in range(2, ws.max_row+1):
        row_is_band=(r%2==0)
        for c in range(1, ws.max_column+1):
            cell=ws.cell(row=r,column=c); cell.alignment=center; cell.border=border
            col_name=columns[c-1] if c-1<len(columns) else ""
            fill, font = _get_conditional_fill_font(col_name, cell.value)
            if fill is not None: cell.fill=fill; cell.font=font if font else cell.font
            elif row_is_band: cell.fill=band_fill
    for col_cells in ws.columns:
        length=max((len(str(c.value)) for c in col_cells if c.value is not None),default=10)
        ws.column_dimensions[col_cells[0].column_letter].width=max(length+2,10)
    ws.freeze_panes="A2"; ws.auto_filter.ref=ws.dimensions

def to_excel_bytes(df, sheet_name="Scan Results"):
    buf=io.BytesIO()
    with pd.ExcelWriter(buf,engine="openpyxl") as writer:
        safe_name=sheet_name[:31]; df.to_excel(writer,index=False,sheet_name=safe_name)
        _format_worksheet(writer.sheets[safe_name], df)
    buf.seek(0); return buf.getvalue()

def to_excel_bytes_multi(sheets):
    buf=io.BytesIO()
    with pd.ExcelWriter(buf,engine="openpyxl") as writer:
        for sheet_name, df in sheets.items():
            if df is None or df.empty: continue
            safe_name=sheet_name[:31]; df.to_excel(writer,index=False,sheet_name=safe_name)
            _format_worksheet(writer.sheets[safe_name], df)
    buf.seek(0); return buf.getvalue()

def to_csv_bytes(df): return df.to_csv(index=False).encode("utf-8")
def to_json_bytes(df): return df.to_json(orient="records",indent=2,force_ascii=False).encode("utf-8")

_INTRADAY_RESOLUTION_MAP = {"5 Minutes":"5","15 Minutes":"15"}

def _is_intraday_candle_closed(candle_time_ist, resolution_minutes):
    return _now_ist() >= candle_time_ist + timedelta(minutes=resolution_minutes)

def _fetch_intraday_cisd_signal(fyers, symbol, resolution, timeframe_label):
    if not isinstance(symbol,str) or not _VALID_EQ_SYMBOL_RE.match(symbol):
        return None, f"{symbol}: invalid symbol format — skipped"
    date_from=(datetime.today()-timedelta(days=INTRADAY_CISD_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    date_to=datetime.today().strftime("%Y-%m-%d")
    resp, err = _safe_history(fyers,{"symbol":symbol,"resolution":resolution,"date_format":"1","range_from":date_from,"range_to":date_to,"cont_flag":"1"})
    if err: return None, f"{symbol}: {err}"
    candles=resp.get("candles") if resp else None
    if not candles or len(candles)<30: return None, None
    try:
        df=pd.DataFrame(candles,columns=["Time","Open","High","Low","Close","Volume"])
        df["Time"]=pd.to_datetime(df["Time"],unit="s",utc=True).dt.tz_convert("Asia/Kolkata")
        df[["Open","High","Low","Close","Volume"]]=df[["Open","High","Low","Close","Volume"]].apply(pd.to_numeric,errors="coerce")
        df=df.dropna(subset=["Open","High","Low","Close"]).sort_values("Time").reset_index(drop=True)
        if len(df)<30: return None, None
        if len(df)>0 and not _is_intraday_candle_closed(df["Time"].iloc[-1], int(resolution)):
            df=df.iloc[:-1].reset_index(drop=True)
        if len(df)<30: return None, None
        smc_structure, cisd_signal, event_ts = _calculate_smc_and_cisd(df)
        if cisd_signal=="None": return None, None
        last_close=float(df["Close"].iloc[-1]); atr=float(calculate_atr(df).iloc[-1])
        if pd.isna(atr) or atr<=0: atr=last_close*0.005
        is_up="Bullish" in cisd_signal
        entry=round(last_close,2)
        sl=round(entry-1.0*atr,2) if is_up else round(entry+1.0*atr,2)
        target=round(entry+2.0*atr,2) if is_up else round(entry-2.0*atr,2)
        risk=abs(entry-sl); reward=abs(target-entry)
        rr_ratio=round(reward/risk,2) if risk>0 else 0.0
        rsi_val=round(float(calculate_rsi(df["Close"]).iloc[-1]),1)
        vol_avg20=df["Volume"].tail(20).mean()
        rvol_raw=round(float(df["Volume"].iloc[-1]/vol_avg20),2) if vol_avg20>0 else 0.0
        ai_score=round(min(max(50+(rvol_raw*10)+(10 if is_up else -10)+(rsi_val-50)*0.3,0),100),1)
        confidence=round(min(95.0,max(35.0,55+min(rvol_raw,3)*8+rr_ratio*3)),1)
        stock_ticker=symbol.replace("NSE:","").replace("-EQ","")
        signal_date_str, signal_time_str = (_format_signal_timestamp(event_ts) if event_ts is not None else _candle_signal_timestamp(df))
        return {"Signal Date":signal_date_str,"Signal Time":signal_time_str,"Timeframe":timeframe_label,"Stock":stock_ticker,"Signal":"🟢 ▲ CISD UP Signal" if is_up else "🔴 ▼ CISD DOWN Signal","Entry":entry,"Stoploss":sl,"Target":target,"Confidence %":confidence,"AI Score":ai_score,"News":calculate_news(stock_ticker,0.0,rvol_raw,"📈 Bullish" if is_up else "📉 Bearish"),"Reason":f"{timeframe_label} CISD {'bullish' if is_up else 'bearish'} shift confirmed (RSI {rsi_val}, RVOL {_format_rvol_display(rvol_raw)})"}, None
    except (KeyError,IndexError,TypeError,ValueError,ZeroDivisionError,AttributeError) as e:
        return None, f"{symbol}: analysis error ({type(e).__name__})"

def run_intraday_cisd_scan(fyers, symbols, resolution, timeframe_label):
    symbols=_validate_symbols(symbols); results,errors=[],[]
    stats=ScanStats(total=len(symbols)); progress=st.progress(0.0,text=f"Scanning Intraday CISD 0 / {len(symbols)}"); done=0
    for i in range(0,len(symbols),BATCH_SIZE):
        batch=symbols[i:i+BATCH_SIZE]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures={executor.submit(_fetch_intraday_cisd_signal,fyers,s,resolution,timeframe_label):s for s in batch}
            for future in as_completed(futures):
                try: res,err=future.result()
                except Exception as e: res,err=None,f"{futures[future]}: worker error ({type(e).__name__})"
                if res: results.append(res)
                if err: errors.append(err)
                stats.record(has_result=bool(res),has_error=bool(err)); done+=1
                progress.progress(done/len(symbols),text=f"Scanning Intraday CISD {done} / {len(symbols)}")
        if i+BATCH_SIZE<len(symbols): time.sleep(BATCH_PAUSE_SECONDS)
    progress.empty(); return results, errors, stats

def _fetch_fo_cisd_signal(fyers, symbol):
    if not isinstance(symbol,str) or not _VALID_EQ_SYMBOL_RE.match(symbol):
        return None, f"{symbol}: invalid symbol format — skipped"
    resp,err=_safe_history(fyers,{"symbol":symbol,"resolution":"D","date_format":"1","range_from":DATE_FROM,"range_to":DATE_TO,"cont_flag":"1"})
    if err: return None, f"{symbol}: {err}"
    candles=resp.get("candles") if resp else None
    if not candles or len(candles)<30: return None, f"{symbol}: insufficient history"
    try:
        df=pd.DataFrame(candles,columns=["Time","Open","High","Low","Close","Volume"])
        df["Time"]=pd.to_datetime(df["Time"],unit="s",utc=True).dt.tz_convert("Asia/Kolkata")
        df[["Open","High","Low","Close","Volume"]]=df[["Open","High","Low","Close","Volume"]].apply(pd.to_numeric,errors="coerce")
        df=df.dropna(subset=["Open","High","Low","Close"])
        if len(df)<30: return None, f"{symbol}: insufficient valid data"
        smc_structure,cisd_signal,event_ts=_calculate_smc_and_cisd(df)
        if cisd_signal=="None": return None, None
        last_close=float(df["Close"].iloc[-1]); atr=float(calculate_atr(df).iloc[-1])
        if pd.isna(atr) or atr<=0: atr=last_close*0.01
        is_bull="Bullish" in cisd_signal
        entry=round(last_close,2)
        sl=round(entry-1.5*atr,2) if is_bull else round(entry+1.5*atr,2)
        target=round(entry+3.0*atr,2) if is_bull else round(entry-3.0*atr,2)
        risk=abs(entry-sl); reward=abs(target-entry); rr_ratio=round(reward/risk,2) if risk>0 else 0.0
        supertrend_label,supertrend_bullish,_=calculate_supertrend(df)
        vol_avg20=df["Volume"].tail(20).mean(); last_volume=float(df["Volume"].iloc[-1])
        rvol_raw=round(last_volume/vol_avg20,2) if vol_avg20>0 else 0.0
        confidence=round(min(95.0,max(35.0,50+min(rvol_raw,3)*10+rr_ratio*3+(10 if supertrend_bullish==is_bull else 0))),1)
        gap_pct=0.0
        if len(df)>=2 and pd.notna(df["Close"].iloc[-2]) and df["Close"].iloc[-2]!=0:
            gap_pct=((df["Open"].iloc[-1]-df["Close"].iloc[-2])/df["Close"].iloc[-2])*100
        stock_ticker=symbol.replace("NSE:","").replace("-EQ","")
        signal_date_str,signal_time_str=(_format_signal_timestamp(event_ts,is_daily=True) if event_ts is not None else _candle_signal_timestamp(df,is_daily=True))
        return {"Signal Date":signal_date_str,"Signal Time":signal_time_str,"Symbol":stock_ticker,"LTP":round(last_close,2),"Signal":"🟢 ▲ CISD BUY" if is_bull else "🔴 ▼ CISD SELL","Entry":entry,"SL":sl,"Target":target,"Confidence":confidence,"Trend":supertrend_label,"Volume":int(last_volume),"RVOL":_format_rvol_display(rvol_raw),"News":calculate_news(stock_ticker,gap_pct,rvol_raw,"📈 Bullish" if is_bull else "📉 Bearish")}, None
    except (KeyError,IndexError,TypeError,ValueError,ZeroDivisionError,AttributeError) as e:
        return None, f"{symbol}: analysis error ({type(e).__name__})"

def run_fo_cisd_scan(fyers, symbols):
    symbols=_validate_symbols(symbols); results,errors=[],[]
    stats=ScanStats(total=len(symbols)); progress=st.progress(0.0,text=f"Scanning F&O CISD 0 / {len(symbols)}"); done=0
    for i in range(0,len(symbols),BATCH_SIZE):
        batch=symbols[i:i+BATCH_SIZE]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures={executor.submit(_fetch_fo_cisd_signal,fyers,s):s for s in batch}
            for future in as_completed(futures):
                try: res,err=future.result()
                except Exception as e: res,err=None,f"{futures[future]}: worker error ({type(e).__name__})"
                if res: results.append(res)
                if err: errors.append(err)
                stats.record(has_result=bool(res),has_error=bool(err)); done+=1
                progress.progress(done/len(symbols),text=f"Scanning F&O CISD {done} / {len(symbols)}")
        if i+BATCH_SIZE<len(symbols): time.sleep(BATCH_PAUSE_SECONDS)
    progress.empty(); return results, errors, stats

def _fetch_golden_death_cross_signal(fyers, symbol):
    if not isinstance(symbol,str) or not _VALID_EQ_SYMBOL_RE.match(symbol):
        return None, f"{symbol}: invalid symbol format — skipped"
    resp,err=_safe_history(fyers,{"symbol":symbol,"resolution":"D","date_format":"1","range_from":DATE_FROM,"range_to":DATE_TO,"cont_flag":"1"})
    if err: return None, f"{symbol}: {err}"
    candles=resp.get("candles") if resp else None
    if not candles or len(candles)<60: return None, f"{symbol}: insufficient history"
    try:
        df=pd.DataFrame(candles,columns=["Time","Open","High","Low","Close","Volume"])
        df["Time"]=pd.to_datetime(df["Time"],unit="s",utc=True).dt.tz_convert("Asia/Kolkata")
        df[["Open","High","Low","Close","Volume"]]=df[["Open","High","Low","Close","Volume"]].apply(pd.to_numeric,errors="coerce")
        df=df.dropna(subset=["Open","High","Low","Close"])
        if len(df)<60: return None, f"{symbol}: insufficient valid data"
        close=df["Close"]
        ema50=close.ewm(span=50,adjust=False).mean()
        ema200=close.ewm(span=200,adjust=False).mean() if len(close)>=200 else close.ewm(span=len(close),adjust=False).mean()
        lookback=min(5,len(close)-1); diff_tail=(ema50-ema200).tail(lookback+1)
        prev_sign=np.sign(diff_tail.iloc[0]); curr_sign=np.sign(diff_tail.iloc[-1])
        if prev_sign<=0 and curr_sign>0: cross_type="Golden Cross"
        elif prev_sign>=0 and curr_sign<0: cross_type="Death Cross"
        else: return None, None
        last_close=float(close.iloc[-1]); atr=float(calculate_atr(df).iloc[-1])
        if pd.isna(atr) or atr<=0: atr=last_close*0.01
        is_bull=cross_type=="Golden Cross"; entry=round(last_close,2)
        if is_bull: sl=round(entry-2.0*atr,2);t1=round(entry+2.0*atr,2);t2=round(entry+3.5*atr,2);t3=round(entry+5.0*atr,2)
        else: sl=round(entry+2.0*atr,2);t1=round(entry-2.0*atr,2);t2=round(entry-3.5*atr,2);t3=round(entry-5.0*atr,2)
        atr_pct=(atr/last_close*100) if last_close else 0
        if atr_pct>=3: holding_days,est_days="3–7 Days",5
        elif atr_pct>=1.5: holding_days,est_days="7–14 Days",10
        else: holding_days,est_days="14–25 Days",18
        exit_date=(_now_ist()+timedelta(days=est_days)).strftime("%d-%b-%Y")
        ema200_last=float(ema200.iloc[-1])
        ema_gap_pct=abs((float(ema50.iloc[-1])-ema200_last)/ema200_last*100) if ema200_last else 0
        trend_strength="🟢 Strong" if ema_gap_pct>=3 else ("🟡 Moderate" if ema_gap_pct>=1 else "🔴 Weak")
        rsi_val=round(float(calculate_rsi(close).iloc[-1]),1)
        vol_avg20=df["Volume"].tail(20).mean()
        rvol_raw=round(float(df["Volume"].iloc[-1]/vol_avg20),2) if vol_avg20>0 else 0.0
        ai_score=round(min(max(50+(15 if is_bull else -15)+(rvol_raw*8)+(rsi_val-50)*0.2,0),100),1)
        confidence=round(min(95.0,max(35.0,55+ema_gap_pct*4+min(rvol_raw,3)*5)),1)
        stock_ticker=symbol.replace("NSE:","").replace("-EQ","")
        signal_date_str,signal_time_str=_candle_signal_timestamp(df,is_daily=True)
        return {"Signal Date":signal_date_str,"Signal Time":signal_time_str,"Stock":stock_ticker,"Cross Type":cross_type,"Signal":"🟢 Swing BUY" if is_bull else "🔴 Swing SELL","Entry":entry,"Stoploss":sl,"Target 1":t1,"Target 2":t2,"Target 3":t3,"Holding Period (Days)":holding_days,"Estimated Exit Date":exit_date,"Trend Strength":trend_strength,"Confidence %":confidence,"AI Score":ai_score,"News":calculate_news(stock_ticker,0.0,rvol_raw,"📈 Bullish" if is_bull else "📉 Bearish")}, None
    except (KeyError,IndexError,TypeError,ValueError,ZeroDivisionError,AttributeError) as e:
        return None, f"{symbol}: analysis error ({type(e).__name__})"

def run_golden_death_cross_scan(fyers, symbols):
    symbols=_validate_symbols(symbols); results,errors=[],[]
    stats=ScanStats(total=len(symbols)); progress=st.progress(0.0,text=f"Scanning Golden/Death Cross 0 / {len(symbols)}"); done=0
    for i in range(0,len(symbols),BATCH_SIZE):
        batch=symbols[i:i+BATCH_SIZE]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures={executor.submit(_fetch_golden_death_cross_signal,fyers,s):s for s in batch}
            for future in as_completed(futures):
                try: res,err=future.result()
                except Exception as e: res,err=None,f"{futures[future]}: worker error ({type(e).__name__})"
                if res: results.append(res)
                if err: errors.append(err)
                stats.record(has_result=bool(res),has_error=bool(err)); done+=1
                progress.progress(done/len(symbols),text=f"Scanning Golden/Death Cross {done} / {len(symbols)}")
        if i+BATCH_SIZE<len(symbols): time.sleep(BATCH_PAUSE_SECONDS)
    progress.empty(); return results, errors, stats

def _fetch_premarket_signal(fyers, symbol):
    if not isinstance(symbol,str) or not _VALID_EQ_SYMBOL_RE.match(symbol):
        return None, f"{symbol}: invalid symbol format — skipped"
    resp,err=_safe_history(fyers,{"symbol":symbol,"resolution":"D","date_format":"1","range_from":DATE_FROM,"range_to":DATE_TO,"cont_flag":"1"})
    if err: return None, f"{symbol}: {err}"
    candles=resp.get("candles") if resp else None
    if not candles or len(candles)<30: return None, f"{symbol}: insufficient history"
    try:
        df=pd.DataFrame(candles,columns=["Time","Open","High","Low","Close","Volume"])
        df["Time"]=pd.to_datetime(df["Time"],unit="s",utc=True).dt.tz_convert("Asia/Kolkata")
        df[["Open","High","Low","Close","Volume"]]=df[["Open","High","Low","Close","Volume"]].apply(pd.to_numeric,errors="coerce")
        df=df.dropna(subset=["Open","High","Low","Close"])
        if len(df)<30: return None, f"{symbol}: insufficient valid data"
        recent=df.tail(10)
        buy_volume=float(recent.loc[recent["Close"]>recent["Open"],"Volume"].sum())
        sell_volume=float(recent.loc[recent["Close"]<=recent["Open"],"Volume"].sum())
        buy_sell_ratio=round(buy_volume/sell_volume,2) if sell_volume>0 else round(buy_volume,2) if buy_volume>0 else 0.0
        gap_pct=0.0
        if len(df)>=2 and pd.notna(df["Close"].iloc[-2]) and df["Close"].iloc[-2]!=0:
            gap_pct=((df["Close"].iloc[-1]-df["Close"].iloc[-2])/df["Close"].iloc[-2])*100
        vol_avg20=df["Volume"].tail(20).mean()
        rvol_raw=round(float(df["Volume"].iloc[-1]/vol_avg20),2) if vol_avg20>0 else 0.0
        rsi_val=round(float(calculate_rsi(df["Close"]).iloc[-1]),1)
        ai_score=round(min(max(50+(buy_sell_ratio-1)*8+(rvol_raw*6)+max(gap_pct,0)*2+(rsi_val-50)*0.2,0),100),1)
        bullish_votes=sum([buy_sell_ratio>1.2,gap_pct>0.3,rvol_raw>=1.5,rsi_val>50])
        bearish_votes=sum([buy_sell_ratio<0.8,gap_pct<-0.3,rvol_raw>=1.5,rsi_val<50])
        if bullish_votes>=3: expected_trend="🟢 Bullish Opening Likely"
        elif bearish_votes>=3: expected_trend="🔴 Bearish Opening Likely"
        else: expected_trend="🟡 Flat/Uncertain"
        stock_ticker=symbol.replace("NSE:","").replace("-EQ","")
        signal_date_str,signal_time_str=_candle_signal_timestamp(df,is_daily=True)
        return {"Signal Date":signal_date_str,"Signal Time":signal_time_str,"Stock":stock_ticker,"Buy Volume":int(buy_volume),"Sell Volume":int(sell_volume),"Buy/Sell Ratio":buy_sell_ratio,"Gap %":f"{gap_pct:.2f}%","RVOL":_format_rvol_display(rvol_raw),"AI Score":ai_score,"Expected Opening Trend":expected_trend,"News":calculate_news(stock_ticker,gap_pct,rvol_raw,"📈 Bullish" if bullish_votes>=3 else ("📉 Bearish" if bearish_votes>=3 else "NO"))}, None
    except (KeyError,IndexError,TypeError,ValueError,ZeroDivisionError,AttributeError) as e:
        return None, f"{symbol}: analysis error ({type(e).__name__})"

def run_premarket_scan(fyers, symbols):
    symbols=_validate_symbols(symbols); results,errors=[],[]
    stats=ScanStats(total=len(symbols)); progress=st.progress(0.0,text=f"Scanning Pre-Market 0 / {len(symbols)}"); done=0
    for i in range(0,len(symbols),BATCH_SIZE):
        batch=symbols[i:i+BATCH_SIZE]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures={executor.submit(_fetch_premarket_signal,fyers,s):s for s in batch}
            for future in as_completed(futures):
                try: res,err=future.result()
                except Exception as e: res,err=None,f"{futures[future]}: worker error ({type(e).__name__})"
                if res: results.append(res)
                if err: errors.append(err)
                stats.record(has_result=bool(res),has_error=bool(err)); done+=1
                progress.progress(done/len(symbols),text=f"Scanning Pre-Market {done} / {len(symbols)}")
        if i+BATCH_SIZE<len(symbols): time.sleep(BATCH_PAUSE_SECONDS)
    progress.empty(); return results, errors, stats

FO_15M_CISD_RESOLUTION="15"; FO_15M_CISD_RESOLUTION_MINUTES=15; FO_15M_CISD_LOOKBACK_DAYS=5

def _fetch_fo_15min_cisd_signal(fyers, symbol):
    if not isinstance(symbol,str) or not _VALID_EQ_SYMBOL_RE.match(symbol):
        return None, f"{symbol}: invalid symbol format — skipped"
    date_from=(datetime.today()-timedelta(days=FO_15M_CISD_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    date_to=datetime.today().strftime("%Y-%m-%d")
    resp,err=_safe_history(fyers,{"symbol":symbol,"resolution":FO_15M_CISD_RESOLUTION,"date_format":"1","range_from":date_from,"range_to":date_to,"cont_flag":"1"})
    if err: return None, f"{symbol}: {err}"
    candles=resp.get("candles") if resp else None
    if not candles or len(candles)<31: return None, None
    try:
        df=pd.DataFrame(candles,columns=["Time","Open","High","Low","Close","Volume"])
        df["Time"]=pd.to_datetime(df["Time"],unit="s",utc=True).dt.tz_convert("Asia/Kolkata")
        df[["Open","High","Low","Close","Volume"]]=df[["Open","High","Low","Close","Volume"]].apply(pd.to_numeric,errors="coerce")
        df=df.dropna(subset=["Open","High","Low","Close"]).sort_values("Time").reset_index(drop=True)
        if len(df)>0 and not _is_intraday_candle_closed(df["Time"].iloc[-1],FO_15M_CISD_RESOLUTION_MINUTES):
            df=df.iloc[:-1].reset_index(drop=True)
        if len(df)<30: return None, None
        smc_structure,cisd_signal,event_ts=_calculate_smc_and_cisd(df)
        if cisd_signal=="None" or event_ts is None: return None, None
        if not _is_intraday_candle_closed(event_ts,FO_15M_CISD_RESOLUTION_MINUTES): return None, None
        last_close=float(df["Close"].iloc[-1]); atr=float(calculate_atr(df).iloc[-1])
        if pd.isna(atr) or atr<=0: atr=last_close*0.005
        is_up="Bullish" in cisd_signal; entry=round(last_close,2)
        if is_up: sl=round(entry-1.0*atr,2);t1=round(entry+1.0*atr,2);t2=round(entry+1.8*atr,2);t3=round(entry+2.6*atr,2)
        else: sl=round(entry+1.0*atr,2);t1=round(entry-1.0*atr,2);t2=round(entry-1.8*atr,2);t3=round(entry-2.6*atr,2)
        risk=abs(entry-sl); reward=abs(t1-entry); rr_ratio=round(reward/risk,2) if risk>0 else 0.0
        rsi_val=round(float(calculate_rsi(df["Close"]).iloc[-1]),1)
        vol_avg20=df["Volume"].tail(20).mean()
        rvol_raw=round(float(df["Volume"].iloc[-1]/vol_avg20),2) if vol_avg20>0 else 0.0
        ai_score=round(min(max(50+(rvol_raw*10)+(10 if is_up else -10)+(rsi_val-50)*0.3,0),100),1)
        confidence=round(min(95.0,max(35.0,55+min(rvol_raw,3)*8+rr_ratio*3)),1)
        stock_ticker=symbol.replace("NSE:","").replace("-EQ","")
        signal_date_str,signal_time_str=_format_signal_timestamp(event_ts,is_daily=False)
        return {"Signal Date":signal_date_str,"Signal Time":signal_time_str,"Stock":stock_ticker,"LTP":round(last_close,2),"CISD Signal":"🟢 ▲ CISD BUY" if is_up else "🔴 ▼ CISD SELL","Entry":entry,"Stop Loss":sl,"Target 1":t1,"Target 2":t2,"Target 3":t3,"Confidence %":confidence,"AI Score":ai_score,"Reason":f"15-Min CISD {'bullish' if is_up else 'bearish'} shift on completed candle (RSI {rsi_val}, RVOL {_format_rvol_display(rvol_raw)})"}, None
    except (KeyError,IndexError,TypeError,ValueError,ZeroDivisionError,AttributeError) as e:
        return None, f"{symbol}: analysis error ({type(e).__name__})"

def run_fo_15min_cisd_scan(fyers, symbols):
    symbols=_validate_symbols(symbols); results,errors=[],[]
    stats=ScanStats(total=len(symbols)); progress=st.progress(0.0,text=f"Scanning F&O 15-Min CISD 0 / {len(symbols)}"); done=0
    for i in range(0,len(symbols),BATCH_SIZE):
        batch=symbols[i:i+BATCH_SIZE]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures={executor.submit(_fetch_fo_15min_cisd_signal,fyers,s):s for s in batch}
            for future in as_completed(futures):
                try: res,err=future.result()
                except Exception as e: res,err=None,f"{futures[future]}: worker error ({type(e).__name__})"
                if res: results.append(res)
                if err: errors.append(err)
                stats.record(has_result=bool(res),has_error=bool(err)); done+=1
                progress.progress(done/len(symbols),text=f"Scanning F&O 15-Min CISD {done} / {len(symbols)}")
        if i+BATCH_SIZE<len(symbols): time.sleep(BATCH_PAUSE_SECONDS)
    progress.empty(); return results, errors, stats

def _load_seen_signal_keys():
    try:
        with open(_SEEN_SIGNALS_FILE,"r",encoding="utf-8") as f:
            data=json.load(f)
        if isinstance(data,list): return set(data)
    except (FileNotFoundError,json.JSONDecodeError,OSError): pass
    return set()

def _save_seen_signal_keys(keys):
    try:
        trimmed=sorted(keys)[-_SEEN_SIGNALS_MAX_KEEP:]
        with open(_SEEN_SIGNALS_FILE,"w",encoding="utf-8") as f:
            json.dump(trimmed,f)
    except OSError as e:
        logger.warning("Could not persist seen-signal keys: %s",e)

def _live_ob_signal_strength(volume_confirmed,smc_aligned,rvol_ok,macd_aligned,supertrend_aligned,ob_strength):
    c=1+int(volume_confirmed)+int(smc_aligned)+int(rvol_ok)+int(macd_aligned)+int(supertrend_aligned)
    if ob_strength=="Strong": c+=1
    return "🟢 Strong" if c>=6 else ("🟡 Medium" if c>=4 else "🔴 Weak")

def _fetch_live_ob_signal(fyers, symbol, seen_keys):
    if not isinstance(symbol,str) or not _VALID_EQ_SYMBOL_RE.match(symbol):
        return None, f"{symbol}: invalid symbol format — skipped"
    date_from=(datetime.today()-timedelta(days=LIVE_OB_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    date_to=datetime.today().strftime("%Y-%m-%d")
    resp,err=_safe_history(fyers,{"symbol":symbol,"resolution":LIVE_OB_RESOLUTION,"date_format":"1","range_from":date_from,"range_to":date_to,"cont_flag":"1"})
    if err: return None, f"{symbol}: {err}"
    candles=resp.get("candles") if resp else None
    if not candles or len(candles)<31: return None, None
    try:
        df=pd.DataFrame(candles,columns=["Time","Open","High","Low","Close","Volume"])
        df["Time"]=pd.to_datetime(df["Time"],unit="s",utc=True).dt.tz_convert("Asia/Kolkata")
        df[["Open","High","Low","Close","Volume"]]=df[["Open","High","Low","Close","Volume"]].apply(pd.to_numeric,errors="coerce")
        df=df.dropna(subset=["Open","High","Low","Close"]).sort_values("Time").reset_index(drop=True)
        if len(df)>0 and not _is_intraday_candle_closed(df["Time"].iloc[-1],LIVE_OB_RESOLUTION_MINUTES):
            df=df.iloc[:-1].reset_index(drop=True)
        if len(df)<30: return None, None
        smc_structure,cisd_signal,event_ts=_calculate_smc_and_cisd(df)
        bullish_ob,bearish_ob,ob_zone,ob_strength=_detect_order_blocks(df,smc_structure)
        if bullish_ob=="No" and bearish_ob=="No": return None, None
        direction="BUY" if bullish_ob!="No" else "SELL"; is_buy=direction=="BUY"
        anchor_ts=event_ts if event_ts is not None else df["Time"].iloc[-1]
        signal_date_str,signal_time_str=_format_signal_timestamp(anchor_ts,is_daily=False)
        dedup_key=f"{symbol}|{LIVE_OB_RESOLUTION}|{signal_date_str}|{signal_time_str}|{direction}"
        if dedup_key in seen_keys: return None, None
        last_close=float(df["Close"].iloc[-1]); atr=float(calculate_atr(df).iloc[-1])
        if pd.isna(atr) or atr<=0: atr=last_close*0.005
        zone_low,zone_high=_parse_ob_zone(ob_zone)
        entry=round(last_close,2)
        if is_buy:
            sl=round((zone_low-0.25*atr) if zone_low is not None else (entry-1.0*atr),2)
            risk=max(entry-sl,0.01); target1=round(entry+1.5*risk,2); target2=round(entry+3.0*risk,2)
        else:
            sl=round((zone_high+0.25*atr) if zone_high is not None else (entry+1.0*atr),2)
            risk=max(sl-entry,0.01); target1=round(entry-1.5*risk,2); target2=round(entry-3.0*risk,2)
        rr_ratio=round(abs(target1-entry)/risk,2) if risk>0 else 0.0
        vol_avg20=float(df["Volume"].tail(20).mean()); last_volume=float(df["Volume"].iloc[-1])
        volume_confirmed=bool(vol_avg20>0 and last_volume>vol_avg20)
        rvol_raw=round(last_volume/vol_avg20,2) if vol_avg20>0 else 0.0
        rsi_val=round(float(calculate_rsi(df["Close"]).iloc[-1]),1)
        macd_line,macd_sig,_=calculate_macd(df["Close"]); macd_bullish=bool(macd_line.iloc[-1]>macd_sig.iloc[-1])
        supertrend_label,supertrend_bullish,_=calculate_supertrend(df)
        smc_aligned=(is_buy and smc_structure in ("BOS 📈","CHOCH 🐂")) or (not is_buy and smc_structure in ("BOS 📉","CHOCH 🐻"))
        macd_aligned=(is_buy and macd_bullish) or (not is_buy and not macd_bullish)
        supertrend_aligned=(is_buy and supertrend_bullish is True) or (not is_buy and supertrend_bullish is False)
        signal_strength=_live_ob_signal_strength(volume_confirmed,smc_aligned,rvol_raw>=1.5,macd_aligned,supertrend_aligned,ob_strength)
        stock_ticker=symbol.replace("NSE:","").replace("-EQ","")
        return {"dedup_key":dedup_key,"Signal Date":signal_date_str,"Signal Time":signal_time_str,"Stock":stock_ticker,"Symbol":symbol,"Direction":direction,"Signal":"🟢 BUY" if is_buy else "🔴 SELL","LTP":entry,"Entry":entry,"Stop Loss":sl,"Target 1":target1,"Target 2":target2,"Risk:Reward":rr_ratio,"Order Block High":zone_high,"Order Block Low":zone_low,"Order Block Zone":ob_zone,"Order Block Strength":ob_strength,"Signal Strength":signal_strength,"Volume Confirmed":"✅ Yes" if volume_confirmed else "❌ No","RVOL":_format_rvol_display(rvol_raw),"RSI":rsi_val,"MACD Signal":"🟢 Bullish" if macd_bullish else "🔴 Bearish","Supertrend":supertrend_label,"SMC Structure":smc_structure,"CISD":cisd_signal}, None
    except Exception as e:
        logger.exception("Live OB error for %s",symbol)
        return None, f"{symbol}: error ({type(e).__name__})"

def _save_signal_txt(row, folder, base_name):
    path=os.path.join(folder,f"{base_name}.txt")
    try:
        lines=[f"NSE Live Order Block Signal — {row['Signal']}","="*48,f"Stock            : {row['Stock']}",f"Signal Date/Time : {row['Signal Date']} {row['Signal Time']}",f"Direction        : {row['Direction']}",f"Entry            : {row['Entry']}",f"Stop Loss        : {row['Stop Loss']}",f"Target 1         : {row['Target 1']}",f"Target 2         : {row['Target 2']}",f"Risk:Reward      : {row['Risk:Reward']}",f"Order Block Zone : {row['Order Block Zone']}",f"Signal Strength  : {row['Signal Strength']}",f"Volume Confirmed : {row['Volume Confirmed']}",f"RVOL             : {row['RVOL']}",f"RSI              : {row['RSI']}",f"MACD Signal      : {row['MACD Signal']}",f"Supertrend       : {row['Supertrend']}",f"SMC Structure    : {row['SMC Structure']}",f"CISD             : {row['CISD']}"]
        with open(path,"w",encoding="utf-8") as f: f.write("\n".join(lines)+"\n")
        return path
    except OSError as e: logger.warning("Could not write TXT for %s: %s",row.get("Stock"),e); return None

def _save_signal_json(row, folder, base_name):
    path=os.path.join(folder,f"{base_name}.json")
    try:
        with open(path,"w",encoding="utf-8") as f: json.dump(row,f,indent=2,default=str)
    except OSError as e: logger.warning("Could not write JSON for %s: %s",row.get("Stock"),e); return None
    try:
        history=[]
        if os.path.exists(_LIVE_OB_MASTER_JSON):
            with open(_LIVE_OB_MASTER_JSON,"r",encoding="utf-8") as f: history=json.load(f)
        history.append(row)
        with open(_LIVE_OB_MASTER_JSON,"w",encoding="utf-8") as f: json.dump(history,f,indent=2,default=str)
    except (OSError,json.JSONDecodeError) as e: logger.warning("Could not append to master JSON: %s",e)
    return path

def _save_signal_csv(row, folder, base_name):
    path=os.path.join(folder,f"{base_name}.csv")
    fieldnames=[k for k in row.keys() if k!="dedup_key"]
    try:
        with open(path,"w",newline="",encoding="utf-8") as f:
            writer=csv.DictWriter(f,fieldnames=fieldnames); writer.writeheader(); writer.writerow({k:row[k] for k in fieldnames})
    except OSError as e: logger.warning("Could not write CSV for %s: %s",row.get("Stock"),e); return None
    try:
        master_exists=os.path.exists(_LIVE_OB_MASTER_CSV)
        with open(_LIVE_OB_MASTER_CSV,"a",newline="",encoding="utf-8") as f:
            writer=csv.DictWriter(f,fieldnames=fieldnames)
            if not master_exists: writer.writeheader()
            writer.writerow({k:row[k] for k in fieldnames})
    except OSError as e: logger.warning("Could not append to master CSV: %s",e)
    return path

def _save_signal_chart(df, row, folder, base_name):
    if not MATPLOTLIB_AVAILABLE: return None
    path=os.path.join(folder,f"{base_name}.png")
    try:
        plot_df=df.tail(60).reset_index(drop=True)
        fig,ax=plt.subplots(figsize=(11,6))
        for i,candle in plot_df.iterrows():
            color="#26a69a" if candle["Close"]>=candle["Open"] else "#ef5350"
            ax.plot([i,i],[candle["Low"],candle["High"]],color=color,linewidth=1)
            ax.add_patch(plt.Rectangle((i-0.3,min(candle["Open"],candle["Close"])),0.6,max(abs(candle["Close"]-candle["Open"]),1e-6),facecolor=color,edgecolor=color))
        zl,zh=row.get("Order Block Low"),row.get("Order Block High")
        if zl is not None and zh is not None: ax.axhspan(zl,zh,color="orange",alpha=0.2,label=f"OB Zone ({zl}-{zh})")
        ax.axhline(row["Entry"],color="blue",linestyle="--",linewidth=1.2,label=f"Entry {row['Entry']}")
        ax.axhline(row["Stop Loss"],color="red",linestyle="--",linewidth=1.2,label=f"SL {row['Stop Loss']}")
        ax.axhline(row["Target 1"],color="green",linestyle="--",linewidth=1.2,label=f"T1 {row['Target 1']}")
        ax.axhline(row["Target 2"],color="darkgreen",linestyle=":",linewidth=1.2,label=f"T2 {row['Target 2']}")
        ax.set_title(f"{row['Stock']} — {row['Signal']} @ {row['Signal Date']} {row['Signal Time']}")
        ax.legend(loc="best",fontsize=8); fig.tight_layout(); fig.savefig(path,dpi=120); plt.close(fig)
        return path
    except Exception as e:
        logger.warning("Could not save chart for %s: %s",row.get("Stock"),e)
        try: plt.close("all")
        except Exception: pass
        return None

def _persist_live_ob_signal(df, row):
    _ensure_app_folders()
    target_folder=SIGNALS_BUY_DIR if row["Direction"]=="BUY" else SIGNALS_SELL_DIR
    safe_time=row["Signal Time"].replace(":","").replace(" ","_")
    base_name=f"{row['Stock']}_{row['Signal Date']}_{safe_time}_{row['Direction']}"
    _save_signal_txt(row,target_folder,base_name); _save_signal_json(row,target_folder,base_name)
    _save_signal_csv(row,target_folder,base_name); _save_signal_chart(df,row,CHARTS_DIR,base_name)
    logger.info("New Live OB signal saved: %s %s @ %s %s",row["Stock"],row["Direction"],row["Signal Date"],row["Signal Time"])

def run_live_ob_signal_scan(fyers, symbols, seen_keys):
    symbols=_validate_symbols(symbols); all_rows,new_rows,errors=[],[],[]
    stats=ScanStats(total=len(symbols)); updated_keys=set(seen_keys)
    progress=st.progress(0.0,text=f"Scanning Live OB Signals 0 / {len(symbols)}"); done=0
    for i in range(0,len(symbols),BATCH_SIZE):
        batch=symbols[i:i+BATCH_SIZE]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures={executor.submit(_fetch_live_ob_signal,fyers,s,seen_keys):s for s in batch}
            for future in as_completed(futures):
                try: res,err=future.result()
                except Exception as e: res,err=None,f"{futures[future]}: worker error ({type(e).__name__})"
                if res:
                    all_rows.append(res)
                    if res["dedup_key"] not in updated_keys: updated_keys.add(res["dedup_key"]); new_rows.append(res)
                if err: errors.append(err)
                stats.record(has_result=bool(res),has_error=bool(err)); done+=1
                progress.progress(done/max(len(symbols),1),text=f"Scanning Live OB Signals {done} / {len(symbols)}")
        if i+BATCH_SIZE<len(symbols): time.sleep(BATCH_PAUSE_SECONDS)
    progress.empty(); _save_seen_signal_keys(updated_keys)
    return all_rows, new_rows, errors, stats, updated_keys

def _persist_new_live_ob_rows(fyers, new_rows):
    for row in new_rows:
        try:
            date_from=(datetime.today()-timedelta(days=LIVE_OB_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
            date_to=datetime.today().strftime("%Y-%m-%d")
            resp,err=_safe_history(fyers,{"symbol":row["Symbol"],"resolution":LIVE_OB_RESOLUTION,"date_format":"1","range_from":date_from,"range_to":date_to,"cont_flag":"1"})
            if err or not resp: continue
            candles=resp.get("candles")
            if not candles: continue
            df=pd.DataFrame(candles,columns=["Time","Open","High","Low","Close","Volume"])
            df["Time"]=pd.to_datetime(df["Time"],unit="s",utc=True).dt.tz_convert("Asia/Kolkata")
            df[["Open","High","Low","Close","Volume"]]=df[["Open","High","Low","Close","Volume"]].apply(pd.to_numeric,errors="coerce")
            df=df.dropna(subset=["Open","High","Low","Close"]).sort_values("Time").reset_index(drop=True)
            _persist_live_ob_signal(df,row)
        except (KeyError,ValueError,TypeError,OSError) as e:
            logger.warning("Could not persist live OB signal for %s: %s",row.get("Stock"),e)

EMA_SWING_RESOLUTION="240"; EMA_SWING_RESOLUTION_MINUTES=240
EMA_SWING_LOOKBACK_DAYS=400; EMA_SWING_FAST_SPAN=50; EMA_SWING_SLOW_SPAN=200; EMA_SWING_MIN_CANDLES=60

def _fetch_ema_swing_signal(fyers, symbol):
    if not isinstance(symbol,str) or not _VALID_EQ_SYMBOL_RE.match(symbol):
        return None, f"{symbol}: invalid symbol format — skipped"
    date_from=(datetime.today()-timedelta(days=EMA_SWING_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    date_to=datetime.today().strftime("%Y-%m-%d")
    resp,err=_safe_history(fyers,{"symbol":symbol,"resolution":EMA_SWING_RESOLUTION,"date_format":"1","range_from":date_from,"range_to":date_to,"cont_flag":"1"})
    if err: return None, f"{symbol}: {err}"
    candles=resp.get("candles") if resp else None
    if not candles or len(candles)<EMA_SWING_MIN_CANDLES: return None, None
    try:
        df=pd.DataFrame(candles,columns=["Time","Open","High","Low","Close","Volume"])
        df["Time"]=pd.to_datetime(df["Time"],unit="s",utc=True).dt.tz_convert("Asia/Kolkata")
        df[["Open","High","Low","Close","Volume"]]=df[["Open","High","Low","Close","Volume"]].apply(pd.to_numeric,errors="coerce")
        df=df.dropna(subset=["Open","High","Low","Close"]).sort_values("Time").reset_index(drop=True)
        if len(df)>0 and not _is_intraday_candle_closed(df["Time"].iloc[-1],EMA_SWING_RESOLUTION_MINUTES):
            df=df.iloc[:-1].reset_index(drop=True)
        if len(df)<EMA_SWING_MIN_CANDLES: return None, None
        close=df["Close"]
        ema50=close.ewm(span=EMA_SWING_FAST_SPAN,adjust=False).mean()
        ema200=close.ewm(span=EMA_SWING_SLOW_SPAN,adjust=False).mean() if len(close)>=EMA_SWING_SLOW_SPAN else close.ewm(span=len(close),adjust=False).mean()
        lookback=min(5,len(close)-1); diff_tail=(ema50-ema200).tail(lookback+1)
        prev_sign=np.sign(diff_tail.iloc[0]); curr_sign=np.sign(diff_tail.iloc[-1])
        if prev_sign<=0 and curr_sign>0: direction="BUY"; golden_cross,death_cross="Yes","No"
        elif prev_sign>=0 and curr_sign<0: direction="SELL"; golden_cross,death_cross="No","Yes"
        else: return None, None
        last_close=float(close.iloc[-1]); ema50_last=round(float(ema50.iloc[-1]),2); ema200_last=round(float(ema200.iloc[-1]),2)
        rsi_val=round(float(calculate_rsi(close).iloc[-1]),1)
        macd_line,macd_signal_line,_=calculate_macd(close); macd_bullish=bool(macd_line.iloc[-1]>macd_signal_line.iloc[-1])
        vol_avg20=float(df["Volume"].tail(20).mean()); last_volume=float(df["Volume"].iloc[-1])
        volume_ok=bool(vol_avg20>0 and last_volume>vol_avg20); volume_ratio=round(last_volume/vol_avg20,2) if vol_avg20>0 else 0.0
        vwap_val=calculate_vwap_approx(df); is_buy=direction=="BUY"
        if is_buy: confs={"RSI>55":rsi_val>55,"MACD Bullish":macd_bullish,"Volume>20avg":volume_ok,"Price>VWAP":vwap_val is not None and last_close>vwap_val}
        else: confs={"RSI<45":rsi_val<45,"MACD Bearish":not macd_bullish,"Volume>20avg":volume_ok,"Price<VWAP":vwap_val is not None and last_close<vwap_val}
        confirmed_count=sum(confs.values()); all_confirmed=confirmed_count==len(confs)
        if all_confirmed and is_buy: trade_decision="🟢 BUY"
        elif all_confirmed and not is_buy: trade_decision="🔴 SELL"
        else: trade_decision="🟡 WATCH"
        ds=1 if is_buy else -1
        ai_score=round(min(max(50+ds*(confirmed_count*10)+ds*min(volume_ratio,3)*3,0),100),1)
        if confirmed_count==4: swing_trend="🟢🟢 Strong Bullish Reversal" if is_buy else "🔴🔴 Strong Bearish Reversal"
        elif confirmed_count>=2: swing_trend="🟢 Bullish Bias" if is_buy else "🔴 Bearish Bias"
        else: swing_trend="🟡 Weak / Unconfirmed"
        atr=float(calculate_atr(df).iloc[-1])
        if pd.isna(atr) or atr<=0: atr=last_close*0.01
        entry=round(last_close,2)
        if is_buy: stop_loss=round(entry-2.0*atr,2);target1=round(entry+2.0*atr,2);target2=round(entry+3.5*atr,2)
        else: stop_loss=round(entry+2.0*atr,2);target1=round(entry-2.0*atr,2);target2=round(entry-3.5*atr,2)
        stock_ticker=symbol.replace("NSE:","").replace("-EQ","")
        signal_date_str,signal_time_str=_candle_signal_timestamp(df,is_daily=False)
        return {"Signal Date":signal_date_str,"Signal Time":signal_time_str,"Stock":stock_ticker,"LTP":round(last_close,2),"EMA 50":ema50_last,"EMA 200":ema200_last,"Golden Cross":golden_cross,"Death Cross":death_cross,"RSI":rsi_val,"MACD":"🟢 Bullish" if macd_bullish else "🔴 Bearish","Volume Ratio":volume_ratio,"VWAP":vwap_val,"AI Score":ai_score,"Swing Trend":swing_trend,"Entry":entry,"Stop Loss":stop_loss,"Target 1":target1,"Target 2":target2,"Trade Decision":trade_decision}, None
    except Exception as e:
        return None, f"{symbol}: error ({type(e).__name__})"

def run_ema_swing_scan(fyers, symbols):
    symbols=_validate_symbols(symbols); results,errors=[],[]
    stats=ScanStats(total=len(symbols)); progress=st.progress(0.0,text=f"Scanning EMA 50/200 Swing (4H) 0 / {len(symbols)}"); done=0
    for i in range(0,len(symbols),BATCH_SIZE):
        batch=symbols[i:i+BATCH_SIZE]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures={executor.submit(_fetch_ema_swing_signal,fyers,s):s for s in batch}
            for future in as_completed(futures):
                try: res,err=future.result()
                except Exception as e: res,err=None,f"{futures[future]}: worker error ({type(e).__name__})"
                if res: results.append(res)
                if err: errors.append(err)
                stats.record(has_result=bool(res),has_error=bool(err)); done+=1
                progress.progress(done/max(len(symbols),1),text=f"Scanning EMA 50/200 Swing (4H) {done} / {len(symbols)}")
        if i+BATCH_SIZE<len(symbols): time.sleep(BATCH_PAUSE_SECONDS)
    progress.empty(); return results, errors, stats

def show_scanner(fyers):
    st.title("🚀 NSE AI PRO V13 — Institutional Scanner")
    st.caption(f"🕒 Current Time (IST): {_now_ist().strftime('%d-%b-%Y %H:%M:%S')} IST")
    symbols = load_nse_equity_symbols()
    st.caption(f"Loaded {len(symbols)} NSE equity symbols from Fyers symbol master.")
    if not symbols:
        st.warning("No symbols loaded — check network access to public.fyers.in.")
        return

    col1,col2,col3 = st.columns([1,1,2])
    with col1:
        limit = st.number_input("Limit symbols (0 = all)", min_value=0, max_value=len(symbols), value=200, step=50)
    with col2:
        enable_xgboost = st.checkbox("Enable XGBoost ML training", value=False, disabled=not XGBOOST_AVAILABLE)
    with col3:
        st.caption(f"~{((limit or len(symbols))/MAX_WORKERS)*0.3/60:.1f}–{((limit or len(symbols))/MAX_WORKERS)*1.0/60:.1f} min estimated.")
    scan_universe = symbols if limit==0 else symbols[:limit]

    if st.button(f"🚀 Run Scan ({len(scan_universe)} symbols)"):
        with st.spinner("Fetching NIFTY benchmark…"):
            nifty_close = fetch_nifty_benchmark(fyers)
        with st.spinner("Scanning…"):
            results,errors,stats = run_scan(fyers, scan_universe, nifty_close, enable_xgboost)
            full_df = pd.DataFrame(results)
            if not full_df.empty and "_Is_High_Quality" in full_df.columns:
                full_df = full_df[full_df["_Is_High_Quality"]==True]
            display_cols = [c for c in full_df.columns if not c.startswith("_")]
            scan_df = full_df[display_cols] if not full_df.empty else full_df
            intraday_df = pd.DataFrame([calculate_intraday_signal(r) for r in results])
            swing_df = pd.DataFrame([calculate_swing_signal(r) for r in results])
        st.session_state["scan_df"]=scan_df; st.session_state["intraday_df"]=intraday_df
        st.session_state["swing_df"]=swing_df; st.session_state["scan_errors"]=errors; st.session_state["scan_stats"]=stats

    if "scan_stats" in st.session_state:
        _display_scan_summary(st.session_state["scan_stats"])

    (tab_scanner,tab_intraday,tab_swing,tab_fo,tab_intraday_cisd,tab_fo_cisd,
     tab_golden_death,tab_premarket,tab_fo_15m_cisd,tab_live_ob,tab_ema_swing,tab_institutional) = st.tabs([
        "📊 Full Scanner","⚡ Intraday Scanner","📈 Swing Trade Scanner","🏛️ F&O Stocks Scanner",
        "🕐 Intraday CISD Signals","🎯 F&O CISD Scanner","✝️ Swing Trading (Golden/Death Cross)",
        "🌅 Pre-Market Scanner","🎯 NSE F&O 15-Min CISD Scanner","🔔 Live OB Signal Scanner",
        "🌟 EMA 50/200 Swing (4H)","🏆 Institutional Scanner",
    ])

    with tab_scanner:
        st.caption(f"High-Quality signals only — ≥{SIGNAL_QUALITY_MIN_CONFIRMATIONS}/10 conditions confirmed.")
        if "scan_df" in st.session_state:
            df = st.session_state["scan_df"]
            if df.empty: st.info("No stocks met the high-quality bar for this scan.")
            else:
                sorted_df = df.sort_values("AI Score",ascending=False)
                st.dataframe(_style_dataframe(sorted_df), use_container_width=True, height=500)
                st.bar_chart(df.set_index("Stock")["AI Score"])
                st.download_button("📥 Download Full Scan as Excel", data=to_excel_bytes(sorted_df,"Scan Results"), file_name=f"nse_scan_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="dl_scan")
        else: st.info("Run a scan above to see Full Scanner results here.")

    with tab_intraday:
        idf = st.session_state.get("intraday_df")
        if idf is not None and not idf.empty:
            idf_sorted = idf.sort_values("Confidence %",ascending=False)
            st.dataframe(_style_dataframe(idf_sorted), use_container_width=True, height=500)
            st.download_button("📥 Download Intraday Signals as Excel", data=to_excel_bytes(idf_sorted,"Intraday Signals"), file_name=f"nse_intraday_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="dl_intraday")
        else: st.info("Run a scan above to see Intraday Scanner results here.")

    with tab_swing:
        sdf = st.session_state.get("swing_df")
        if sdf is not None and not sdf.empty:
            sdf_sorted = sdf.sort_values("Confidence %",ascending=False)
            st.dataframe(_style_dataframe(sdf_sorted), use_container_width=True, height=500)
            st.download_button("📥 Download Swing Signals as Excel", data=to_excel_bytes(sdf_sorted,"Swing Signals"), file_name=f"nse_swing_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="dl_swing")
        else: st.info("Run a scan above to see Swing Trade Scanner results here.")

    with tab_fo:
        fo_symbols = load_nse_fo_stock_symbols()
        st.caption(f"Loaded {len(fo_symbols)} F&O-permitted NSE stocks.")
        if not fo_symbols: st.warning("No F&O symbols loaded.")
        else:
            fo_col1,fo_col2 = st.columns([1,1])
            with fo_col1: fo_limit = st.number_input("Limit F&O symbols (0=all)", min_value=0, max_value=len(fo_symbols), value=len(fo_symbols), step=25, key="fo_limit")
            with fo_col2: fo_enable_xgboost = st.checkbox("Enable XGBoost (F&O)", value=False, key="fo_xgb", disabled=not XGBOOST_AVAILABLE)
            fo_universe = fo_symbols if fo_limit==0 else fo_symbols[:fo_limit]
            if st.button(f"🏛️ Run F&O Stocks Scan ({len(fo_universe)} symbols)", key="fo_run"):
                with st.spinner("Scanning F&O stocks…"):
                    fo_nifty_close = fetch_nifty_benchmark(fyers)
                    fo_results,fo_errors,fo_stats = run_scan(fyers,fo_universe,fo_nifty_close,fo_enable_xgboost)
                    fo_full_df = pd.DataFrame(fo_results)
                    if not fo_full_df.empty and "_Is_High_Quality" in fo_full_df.columns:
                        fo_full_df = fo_full_df[fo_full_df["_Is_High_Quality"]==True]
                    fo_dc = [c for c in fo_full_df.columns if not c.startswith("_")]
                    fo_scan_df = fo_full_df[fo_dc] if not fo_full_df.empty else fo_full_df
                st.session_state["fo_scan_df"]=fo_scan_df; st.session_state["fo_scan_errors"]=fo_errors; st.session_state["fo_scan_stats"]=fo_stats
            if "fo_scan_stats" in st.session_state: _display_scan_summary(st.session_state["fo_scan_stats"])
            fo_df = st.session_state.get("fo_scan_df")
            if fo_df is not None and not fo_df.empty:
                fo_sorted = fo_df.sort_values("AI Score",ascending=False)
                st.dataframe(_style_dataframe(fo_sorted), use_container_width=True, height=500)
                st.bar_chart(fo_df.set_index("Stock")["AI Score"])
                st.download_button("📥 Download F&O Scan as Excel", data=to_excel_bytes(fo_sorted,"F&O Stocks"), file_name=f"nse_fo_scan_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="dl_fo")
            elif "fo_scan_df" in st.session_state: st.info("No F&O stocks met the high-quality bar.")
            else: st.info("Run an F&O scan above.")
            if st.session_state.get("fo_scan_errors"):
                with st.expander(f"⚠️ Skipped F&O symbols ({len(st.session_state['fo_scan_errors'])})"): st.text("\n".join(st.session_state["fo_scan_errors"][:20]))

    with tab_intraday_cisd:
        ic1,ic2,_ = st.columns([1,1,1])
        with ic1: icisd_tf = st.selectbox("Timeframe", list(_INTRADAY_RESOLUTION_MAP.keys()), key="icisd_timeframe")
        with ic2: icisd_lim = st.number_input("Limit (0=all)", min_value=0, max_value=len(symbols), value=min(200,len(symbols)), step=50, key="icisd_limit")
        icisd_universe = symbols if icisd_lim==0 else symbols[:icisd_lim]
        if st.button(f"🕐 Run Intraday CISD Scan ({len(icisd_universe)} symbols, {icisd_tf})", key="icisd_run"):
            with st.spinner("Scanning…"):
                icisd_results,icisd_errors,icisd_stats = run_intraday_cisd_scan(fyers,icisd_universe,_INTRADAY_RESOLUTION_MAP[icisd_tf],icisd_tf)
                st.session_state["intraday_cisd_df"]=pd.DataFrame(icisd_results); st.session_state["intraday_cisd_errors"]=icisd_errors; st.session_state["intraday_cisd_stats"]=icisd_stats
        if "intraday_cisd_stats" in st.session_state: _display_scan_summary(st.session_state["intraday_cisd_stats"])
        icisd_df = st.session_state.get("intraday_cisd_df")
        if icisd_df is not None and not icisd_df.empty:
            st.dataframe(_style_dataframe(icisd_df.sort_values("Confidence %",ascending=False)), use_container_width=True, height=500)
            st.download_button("📥 Download as Excel", data=to_excel_bytes(icisd_df,"Intraday CISD"), file_name=f"nse_intraday_cisd_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="dl_icisd")
        else: st.info("Run an Intraday CISD scan above.")
        if st.session_state.get("intraday_cisd_errors"):
            with st.expander(f"⚠️ Skipped ({len(st.session_state['intraday_cisd_errors'])})"): st.text("\n".join(st.session_state["intraday_cisd_errors"][:20]))

    with tab_fo_cisd:
        fo_cisd_symbols = load_nse_fo_stock_symbols()
        if not fo_cisd_symbols: st.warning("No F&O symbols loaded.")
        else:
            fo_cisd_lim = st.number_input("Limit (0=all)", min_value=0, max_value=len(fo_cisd_symbols), value=len(fo_cisd_symbols), step=25, key="fo_cisd_limit")
            fo_cisd_universe = fo_cisd_symbols if fo_cisd_lim==0 else fo_cisd_symbols[:fo_cisd_lim]
            if st.button(f"🎯 Run F&O CISD Scan ({len(fo_cisd_universe)} symbols)", key="fo_cisd_run"):
                with st.spinner("Scanning…"):
                    fo_cisd_results,fo_cisd_errors,fo_cisd_stats = run_fo_cisd_scan(fyers,fo_cisd_universe)
                    st.session_state["fo_cisd_df"]=pd.DataFrame(fo_cisd_results); st.session_state["fo_cisd_errors"]=fo_cisd_errors; st.session_state["fo_cisd_stats"]=fo_cisd_stats
            if "fo_cisd_stats" in st.session_state: _display_scan_summary(st.session_state["fo_cisd_stats"])
            fo_cisd_df = st.session_state.get("fo_cisd_df")
            if fo_cisd_df is not None and not fo_cisd_df.empty:
                st.dataframe(_style_dataframe(fo_cisd_df.sort_values("Confidence",ascending=False)), use_container_width=True, height=500)
                st.download_button("📥 Download as Excel", data=to_excel_bytes(fo_cisd_df,"F&O CISD"), file_name=f"nse_fo_cisd_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="dl_fo_cisd")
            else: st.info("Run an F&O CISD scan above.")
            if st.session_state.get("fo_cisd_errors"):
                with st.expander(f"⚠️ Skipped ({len(st.session_state['fo_cisd_errors'])})"): st.text("\n".join(st.session_state["fo_cisd_errors"][:20]))

    with tab_golden_death:
        gd_lim = st.number_input("Limit (0=all)", min_value=0, max_value=len(symbols), value=min(300,len(symbols)), step=50, key="gd_limit")
        gd_universe = symbols if gd_lim==0 else symbols[:gd_lim]
        if st.button(f"✝️ Run Golden/Death Cross Scan ({len(gd_universe)} symbols)", key="gd_run"):
            with st.spinner("Scanning…"):
                gd_results,gd_errors,gd_stats = run_golden_death_cross_scan(fyers,gd_universe)
                st.session_state["golden_death_df"]=pd.DataFrame(gd_results); st.session_state["golden_death_errors"]=gd_errors; st.session_state["golden_death_stats"]=gd_stats
        if "golden_death_stats" in st.session_state: _display_scan_summary(st.session_state["golden_death_stats"])
        gd_df = st.session_state.get("golden_death_df")
        if gd_df is not None and not gd_df.empty:
            st.dataframe(_style_dataframe(gd_df.sort_values("Confidence %",ascending=False)), use_container_width=True, height=500)
            st.download_button("📥 Download as Excel", data=to_excel_bytes(gd_df,"Swing Golden-Death"), file_name=f"nse_golden_death_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="dl_gd")
        else: st.info("Run a Golden/Death Cross scan above.")
        if st.session_state.get("golden_death_errors"):
            with st.expander(f"⚠️ Skipped ({len(st.session_state['golden_death_errors'])})"): st.text("\n".join(st.session_state["golden_death_errors"][:20]))

    with tab_premarket:
        st.caption("⚠️ Buy/Sell Volume is a proxy from last 10 sessions — not live tick data.")
        pm_lim = st.number_input("Limit (0=all)", min_value=0, max_value=len(symbols), value=min(300,len(symbols)), step=50, key="pm_limit")
        pm_universe = symbols if pm_lim==0 else symbols[:pm_lim]
        if st.button(f"🌅 Run Pre-Market Scan ({len(pm_universe)} symbols)", key="pm_run"):
            with st.spinner("Scanning…"):
                pm_results,pm_errors,pm_stats = run_premarket_scan(fyers,pm_universe)
                st.session_state["premarket_df"]=pd.DataFrame(pm_results); st.session_state["premarket_errors"]=pm_errors; st.session_state["premarket_stats"]=pm_stats
        if "premarket_stats" in st.session_state: _display_scan_summary(st.session_state["premarket_stats"])
        pm_df = st.session_state.get("premarket_df")
        if pm_df is not None and not pm_df.empty:
            pm_filter = st.selectbox("Filter", ["All","Bullish Candidates","Bearish Candidates","High RVOL","Gap Up","Gap Down"], key="pm_filter")
            pm_view = pm_df.copy()
            try:
                if pm_filter=="Bullish Candidates": pm_view=pm_view[pm_view["Expected Opening Trend"].str.contains("Bullish",na=False)]
                elif pm_filter=="Bearish Candidates": pm_view=pm_view[pm_view["Expected Opening Trend"].str.contains("Bearish",na=False)]
                elif pm_filter=="High RVOL": pm_view=pm_view[pm_view["RVOL"].str.contains("❤️|🔥",na=False,regex=True)]
                elif pm_filter=="Gap Up": pm_view=pm_view[pm_view["Gap %"].str.replace("%","",regex=False).astype(float)>0]
                elif pm_filter=="Gap Down": pm_view=pm_view[pm_view["Gap %"].str.replace("%","",regex=False).astype(float)<0]
            except Exception: pm_view=pm_df.copy()
            st.dataframe(_style_dataframe(pm_view.sort_values("AI Score",ascending=False)), use_container_width=True, height=500)
            st.download_button("📥 Download as Excel", data=to_excel_bytes(pm_view,"Pre-Market"), file_name=f"nse_premarket_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="dl_pm")
        else: st.info("Run a Pre-Market scan above.")
        if st.session_state.get("premarket_errors"):
            with st.expander(f"⚠️ Skipped ({len(st.session_state['premarket_errors'])})"): st.text("\n".join(st.session_state["premarket_errors"][:20]))

    with tab_fo_15m_cisd:
        fo15_symbols = load_nse_fo_stock_symbols()
        st.caption(f"Loaded {len(fo15_symbols)} F&O-permitted NSE stocks.")
        if not fo15_symbols: st.warning("No F&O symbols loaded.")
        else:
            fo15_lim = st.number_input("Limit (0=all)", min_value=0, max_value=len(fo15_symbols), value=len(fo15_symbols), step=25, key="fo15_limit")
            fo15_universe = fo15_symbols if fo15_lim==0 else fo15_symbols[:fo15_lim]
            if st.button(f"🎯 Run F&O 15-Min CISD Scan ({len(fo15_universe)} symbols)", key="fo15_run"):
                with st.spinner("Scanning…"):
                    fo15_results,fo15_errors,fo15_stats = run_fo_15min_cisd_scan(fyers,fo15_universe)
                    st.session_state["fo15_cisd_df"]=pd.DataFrame(fo15_results); st.session_state["fo15_cisd_errors"]=fo15_errors; st.session_state["fo15_cisd_stats"]=fo15_stats
            if "fo15_cisd_stats" in st.session_state: _display_scan_summary(st.session_state["fo15_cisd_stats"])
            fo15_df = st.session_state.get("fo15_cisd_df")
            if fo15_df is not None and not fo15_df.empty:
                st.dataframe(_style_dataframe(fo15_df.sort_values("Confidence %",ascending=False)), use_container_width=True, height=500)
                st.download_button("📥 Download as Excel", data=to_excel_bytes(fo15_df,"F&O 15-Min CISD"), file_name=f"nse_fo_15min_cisd_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="dl_fo15")
            else: st.info("Run an F&O 15-Min CISD scan above.")
            if st.session_state.get("fo15_cisd_errors"):
                with st.expander(f"⚠️ Skipped ({len(st.session_state['fo15_cisd_errors'])})"): st.text("\n".join(st.session_state["fo15_cisd_errors"][:20]))

    with tab_live_ob:
        st.caption("Live 15-minute Order Block BUY/SELL signal engine.")
        lob_col1,lob_col2 = st.columns([2,1])
        with lob_col1:
            lob_lim = st.number_input("Limit symbols (0=ALL)", min_value=0, max_value=len(symbols), value=0, step=50, key="live_ob_limit")
            lob_watchlist = symbols if lob_lim==0 else symbols[:lob_lim]
            st.caption(f"Monitoring {len(lob_watchlist)} stocks.")
        with lob_col2:
            lob_auto = st.checkbox(f"🔁 Auto-refresh every {LIVE_OB_AUTO_REFRESH_SECONDS}s", value=False, key="live_ob_auto_refresh")
        run_lob_now = st.button(f"🔔 Run Live OB Scan ({len(lob_watchlist)} symbols)", key="live_ob_run")
        if run_lob_now or lob_auto:
            if not lob_watchlist: st.warning("No symbols available.")
            else:
                seen_keys = _load_seen_signal_keys()
                with st.spinner("Scanning for live 15-min OB signals…"):
                    lob_rows,lob_new_rows,lob_errors,lob_stats,updated_keys = run_live_ob_signal_scan(fyers,lob_watchlist,seen_keys)
                    if lob_new_rows: _persist_new_live_ob_rows(fyers,lob_new_rows)
                st.session_state["live_ob_df"]=pd.DataFrame([{k:v for k,v in r.items() if k not in ("dedup_key","Symbol")} for r in lob_rows])
                st.session_state["live_ob_errors"]=lob_errors; st.session_state["live_ob_stats"]=lob_stats
                st.session_state["live_ob_last_run"]=_now_ist().strftime("%d-%b-%Y %H:%M:%S")
                for new_row in lob_new_rows:
                    msg=f"{new_row['Signal']} {new_row['Stock']} @ {new_row['Entry']} (SL {new_row['Stop Loss']}, T1 {new_row['Target 1']}, T2 {new_row['Target 2']}, RR {new_row['Risk:Reward']}) — {new_row['Signal Date']} {new_row['Signal Time']}"
                    (st.success if new_row["Direction"]=="BUY" else st.error)(f"{'🟢 NEW BUY' if new_row['Direction']=='BUY' else '🔴 NEW SELL'} SIGNAL: {msg}")
                    try: st.toast(msg, icon="🔔")
                    except Exception: pass
        if "live_ob_stats" in st.session_state: _display_scan_summary(st.session_state["live_ob_stats"])
        if st.session_state.get("live_ob_last_run"): st.caption(f"Last scanned: {st.session_state['live_ob_last_run']} IST")
        lob_df = st.session_state.get("live_ob_df")
        if lob_df is not None and not lob_df.empty:
            st.dataframe(_style_dataframe(lob_df.sort_values("Signal Date",ascending=False)), use_container_width=True, height=450)
            dl1,dl2,dl3=st.columns(3)
            with dl1: st.download_button("📥 Download Live OB (Excel)", data=to_excel_bytes(lob_df,"Live OB Signals"), file_name=f"live_ob_signals_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="dl_live_ob_xlsx")
            with dl2:
                if os.path.exists(_LIVE_OB_MASTER_CSV):
                    with open(_LIVE_OB_MASTER_CSV,"rb") as f: st.download_button("📥 All-Time Log (CSV)", data=f.read(), file_name="live_ob_signals_all_time.csv", mime="text/csv", key="dl_live_ob_csv")
            with dl3:
                if os.path.exists(_LIVE_OB_MASTER_JSON):
                    with open(_LIVE_OB_MASTER_JSON,"rb") as f: st.download_button("📥 All-Time Log (JSON)", data=f.read(), file_name="live_ob_signals_all_time.json", mime="application/json", key="dl_live_ob_json")
        else: st.info("Run a Live OB scan above or enable auto-refresh.")
        if st.session_state.get("live_ob_errors"):
            with st.expander(f"⚠️ Skipped ({len(st.session_state['live_ob_errors'])})"): st.text("\n".join(st.session_state["live_ob_errors"][:20]))
        if lob_auto: time.sleep(LIVE_OB_AUTO_REFRESH_SECONDS); st.rerun()

    with tab_ema_swing:
        st.caption("EMA 50/200 on 4H candles. BUY/SELL requires RSI+MACD+Volume+VWAP confirmation; otherwise 🟡 WATCH.")
        es_lim = st.number_input("Limit (0=all)", min_value=0, max_value=len(symbols), value=min(300,len(symbols)), step=50, key="ema_swing_limit")
        es_universe = symbols if es_lim==0 else symbols[:es_lim]
        if st.button(f"🌟 Run EMA 50/200 Swing Scan ({len(es_universe)} symbols, 4H)", key="ema_swing_run"):
            with st.spinner("Scanning 4H candles…"):
                es_results,es_errors,es_stats = run_ema_swing_scan(fyers,es_universe)
                st.session_state["ema_swing_df"]=pd.DataFrame(es_results); st.session_state["ema_swing_errors"]=es_errors; st.session_state["ema_swing_stats"]=es_stats
        if "ema_swing_stats" in st.session_state: _display_scan_summary(st.session_state["ema_swing_stats"])
        es_df = st.session_state.get("ema_swing_df")
        if es_df is not None and not es_df.empty:
            es_filter = st.selectbox("Filter", ["All","BUY only","SELL only","WATCH only"], key="ema_swing_filter")
            es_view = es_df.copy()
            try:
                if es_filter=="BUY only": es_view=es_view[es_view["Trade Decision"].str.contains("BUY",na=False)]
                elif es_filter=="SELL only": es_view=es_view[es_view["Trade Decision"].str.contains("SELL",na=False)]
                elif es_filter=="WATCH only": es_view=es_view[es_view["Trade Decision"].str.contains("WATCH",na=False)]
            except Exception: pass
            es_sorted = es_view.sort_values("AI Score",ascending=False)
            st.dataframe(_style_dataframe(es_sorted), use_container_width=True, height=500)
            ec1,ec2,ec3=st.columns(3)
            with ec1: st.download_button("📥 Excel", data=to_excel_bytes(es_sorted,"EMA Swing 4H"), file_name=f"nse_ema_swing_4h_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="dl_ema_swing_xlsx")
            with ec2: st.download_button("📥 CSV", data=to_csv_bytes(es_sorted), file_name=f"nse_ema_swing_4h_{_now_ist().strftime('%Y%m%d_%H%M')}.csv", mime="text/csv", key="dl_ema_swing_csv")
            with ec3: st.download_button("📥 JSON", data=to_json_bytes(es_sorted), file_name=f"nse_ema_swing_4h_{_now_ist().strftime('%Y%m%d_%H%M')}.json", mime="application/json", key="dl_ema_swing_json")
        else: st.info("Run an EMA 50/200 Swing scan above.")
        if st.session_state.get("ema_swing_errors"):
            with st.expander(f"⚠️ Skipped ({len(st.session_state['ema_swing_errors'])})"): st.text("\n".join(st.session_state["ema_swing_errors"][:20]))

    # ── 🏆 Institutional Signal Scanner (NEW — fixed report rendering) ────────
    with tab_institutional:
        st.markdown(
            "### 🏆 Institutional-Quality Signal Engine\n"
            "**20-Point Validation:** HTF Trend · BOS · CHOCH · CISD · OB Quality · "
            "Fresh OB · Untested OB · Liquidity Sweep · FVG · Volume · Candle · ATR · "
            "Momentum · VWAP · EMA · RSI · MACD · ADX · RR ≥ 1:2  \n"
            "Signals with **AI Confidence < 80%** are automatically rejected.  \n"
            "**Grades:** 🥇 A+ (20/20, ≥93%) · 🥈 A (≥17, ≥88%) · 🥉 B (≥14, ≥80%) · C (≥10, ≥70%) · ⬛ REJECT"
        )
        st.divider()

        # ── Controls ──────────────────────────────────────────────────────────
        inst_c1, inst_c2, inst_c3, inst_c4 = st.columns([1, 1, 1, 2])
        with inst_c1:
            inst_lim = st.number_input(
                "Limit symbols (0 = all)", min_value=0, max_value=len(symbols),
                value=min(200, len(symbols)), step=50, key="inst_limit"
            )
        with inst_c2:
            inst_xgb = st.checkbox(
                "Enable XGBoost ML", value=False, key="inst_xgb",
                disabled=not XGBOOST_AVAILABLE
            )
        with inst_c3:
            inst_gf = st.selectbox(
                "Grade Filter",
                ["All (A+ to C)", "A+ only", "A+ and A", "A+ A B"],
                key="inst_grade_filter"
            )
        with inst_c4:
            inst_show_reports = st.checkbox(
                "📋 Show per-signal AI Reports below table", value=True,
                key="inst_show_reports"
            )

        inst_universe = symbols if inst_lim == 0 else symbols[:inst_lim]

        if st.button(f"🏆 Run Institutional Scan ({len(inst_universe)} symbols)", key="inst_run"):
            with st.spinner("Fetching NIFTY benchmark…"):
                inst_nifty = fetch_nifty_benchmark(fyers)
            with st.spinner(f"Running 20-point validation on {len(inst_universe)} symbols…"):
                inst_results, inst_errors, inst_stats = run_scan_enhanced(
                    fyers, inst_universe, inst_nifty, inst_xgb
                )
                inst_full_df = pd.DataFrame(inst_results)
                # Keep ALL signals in session — UI toggle controls filtering below
                # Remove only pure internal helper columns (prefixed _)
                inst_dc = [c for c in inst_full_df.columns if not c.startswith("_")]
                inst_scan_df = inst_full_df[inst_dc] if not inst_full_df.empty else inst_full_df
                # Also store the strict-pass-only version separately
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

        # ── Scan summary ──────────────────────────────────────────────────────
        if "inst_stats" in st.session_state:
            _display_scan_summary(st.session_state["inst_stats"])

        # ── Results ───────────────────────────────────────────────────────────
        inst_df = st.session_state.get("inst_scan_df")

        # ── Debug info: always show raw counts so user can see what the scan found ──
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

        # Toggle to bypass confidence filter (shows everything the scan produced)
        inst_show_all = st.checkbox(
            "🔓 Show ALL signals (bypass 80% confidence filter)",
            value=True,
            key="inst_show_all",
            help="When ON: shows every signal the scan produced regardless of confidence. "
                 "When OFF: only signals that passed the strict 80% threshold are shown."
        )

        if inst_df is not None and not inst_df.empty:
            # Choose base: all signals OR strict-pass-only
            if inst_show_all:
                view_df = inst_df.copy()
            else:
                strict_df = st.session_state.get("inst_strict_df", inst_df)
                view_df = strict_df.copy() if strict_df is not None and not strict_df.empty else inst_df.copy()

            # Apply grade filter on top
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
                    st.warning(
                        f"⚠️ Grade filter **'{inst_gf}'** returned 0 rows — showing all grades instead.  \n"
                        "Change the Grade Filter dropdown to see specific grades."
                    )
            except (KeyError, TypeError):
                pass

            if view_df.empty:
                st.warning(
                    "⚠️ No signals to display.  \n"
                    "💡 Enable **'Show ALL signals'** toggle above, or increase the symbol limit."
                )
            else:
                view_sorted = view_df.sort_values("AI Confidence %", ascending=False).reset_index(drop=True)

                # ── KPI metrics ───────────────────────────────────────────────
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

                # ── Main data table ───────────────────────────────────────────
                st.markdown("#### 📋 Signal Table")

                # Preferred column order — AI Report excluded (shown in cards below)
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
                    "Signal Reason",
                ]
                # Build column list — exclude AI Report and internal cols
                _exclude = {"AI Report", "Signal Reason"}
                existing_priority = [c for c in priority_cols if c in view_sorted.columns and c not in _exclude]
                remaining = [c for c in view_sorted.columns if c not in existing_priority and c not in _exclude]
                table_cols = existing_priority + remaining

                # Convert all values to strings to prevent Styler type errors
                table_df = view_sorted[table_cols].copy()
                for _col in table_df.columns:
                    if table_df[_col].dtype == object:
                        table_df[_col] = table_df[_col].fillna("—").astype(str)
                    else:
                        table_df[_col] = table_df[_col].fillna(0)

                # Safe styled render with plain fallback
                try:
                    st.dataframe(
                        _style_dataframe(table_df),
                        use_container_width=True,
                        height=500,
                    )
                except Exception:
                    # Plain fallback — always works
                    st.dataframe(table_df, use_container_width=True, height=500)

                # ── Download buttons ──────────────────────────────────────────
                st.markdown("#### 💾 Export")
                idl1, idl2, idl3 = st.columns(3)
                with idl1:
                    st.download_button(
                        "📥 Download as Excel",
                        data=to_excel_bytes(view_sorted, "Institutional Signals"),
                        file_name=f"nse_institutional_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        key="dl_inst_xlsx",
                    )
                with idl2:
                    st.download_button(
                        "📥 Download as CSV",
                        data=to_csv_bytes(view_sorted),
                        file_name=f"nse_institutional_{_now_ist().strftime('%Y%m%d_%H%M')}.csv",
                        mime="text/csv",
                        key="dl_inst_csv",
                    )
                with idl3:
                    st.download_button(
                        "📥 Download as JSON",
                        data=to_json_bytes(view_sorted),
                        file_name=f"nse_institutional_{_now_ist().strftime('%Y%m%d_%H%M')}.json",
                        mime="application/json",
                        key="dl_inst_json",
                    )

                # ── Per-signal AI Report cards (ALWAYS VISIBLE, not inside expander) ──
                if inst_show_reports:
                    st.divider()
                    st.markdown(f"#### 🧠 AI Signal Reports — {len(view_sorted)} Signal(s)")
                    st.caption("Each card shows the full institutional analysis for that signal.")

                    for idx, row in view_sorted.iterrows():
                        stock      = str(row.get("Stock", "?"))
                        decision   = str(row.get("Enhanced Decision", "—"))
                        grade      = str(row.get("Signal Grade", "?"))
                        confidence = row.get("AI Confidence %", 0)
                        passed     = row.get("Confirmations Passed", "?")
                        failed_str = str(row.get("Confirmations Failed", "None"))
                        ltp        = row.get("LTP", "—")
                        entry      = row.get("Enhanced Entry", "—")
                        sl         = row.get("Enhanced SL", "—")
                        t1         = row.get("Enhanced Target 1", "—")
                        t2         = row.get("Enhanced Target 2", "—")
                        t3         = row.get("Enhanced Target 3", "—")
                        rr         = row.get("Enhanced RR", "—")
                        sig_date   = row.get("Signal Date", "—")
                        sig_time   = row.get("Signal Time", "—")
                        reason_raw = str(row.get("Signal Reason", "—"))
                        ai_report  = str(row.get("AI Report", ""))
                        htf        = str(row.get("HTF Trend", "—"))
                        fvg_lbl    = str(row.get("FVG", "—"))
                        liq        = str(row.get("Liquidity Sweep", "—"))
                        ob_bull    = str(row.get("OB Type (Bullish)", "—"))
                        ob_bear    = str(row.get("OB Type (Bearish)", "—"))
                        adx_val    = row.get("ADX", "—")
                        momentum   = str(row.get("Momentum", "—"))

                        # Grade colour
                        grade_color = {
                            "A+": "#006100", "A": "#1a7a1a", "B": "#ff8c00",
                            "C": "#cc6600", "REJECT": "#888888"
                        }.get(grade, "#333333")

                        is_buy = "BUY" in decision

                        # Card header
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
<span style="color:#555;font-size:13px">✅ {passed}/20 confirmations &nbsp;|&nbsp; 📅 {sig_date} {sig_time}</span>
</div>
""",
                            unsafe_allow_html=True,
                        )

                        # Trade levels
                        col_a, col_b, col_c, col_d = st.columns(4)
                        col_a.metric("LTP", f"₹{ltp}")
                        col_a.metric("Entry", f"₹{entry}")
                        col_b.metric("Stop Loss 🔴", f"₹{sl}")
                        col_b.metric("R:R", f"1:{rr}")
                        col_c.metric("Target 1 🎯", f"₹{t1}")
                        col_c.metric("Target 2 🎯", f"₹{t2}")
                        col_d.metric("Target 3 🚀", f"₹{t3}")
                        col_d.metric("ADX", f"{adx_val}")

                        # Context row
                        ctx1, ctx2, ctx3, ctx4 = st.columns(4)
                        ctx1.info(f"**HTF Trend**\n\n{htf}")
                        ctx2.info(f"**FVG**\n\n{fvg_lbl}")
                        ctx3.info(f"**Liquidity**\n\n{liq}")
                        ctx4.info(f"**Momentum**\n\n{momentum}")

                        ob1, ob2 = st.columns(2)
                        ob1.success(f"**Bullish OB:** {ob_bull}") if "Fresh" in ob_bull or "Institutional" in ob_bull else ob1.warning(f"**Bullish OB:** {ob_bull}")
                        ob2.error(f"**Bearish OB:** {ob_bear}") if "Fresh" in ob_bear or "Institutional" in ob_bear else ob2.warning(f"**Bearish OB:** {ob_bear}")

                        # ✓ Reasons why this signal was generated
                        st.markdown("**✅ Why this signal was generated:**")
                        reasons = [r.strip() for r in reason_raw.split(" | ") if r.strip() and r.strip() != "—"]
                        if reasons:
                            reason_cols = st.columns(min(len(reasons), 3))
                            for i, reason in enumerate(reasons):
                                reason_cols[i % 3].success(f"✓ {reason}")
                        else:
                            st.caption("No specific reasons recorded.")

                        # Failed confirmations
                        if failed_str and failed_str.lower() not in ("none", "—", ""):
                            with st.expander(f"❌ Failed confirmations ({failed_str.count(',') + 1 if ',' in failed_str else 1})"):
                                for f_item in failed_str.split(","):
                                    f_item = f_item.strip()
                                    if f_item:
                                        st.markdown(f"- ❌ {f_item}")

                        # Full AI Report — parsed line-by-line into a table
                        st.markdown("**📄 Full AI Analysis Report:**")
                        if ai_report and ai_report not in ("—", ""):
                            report_lines = [ln.strip() for ln in ai_report.split(" | ") if ln.strip()]
                            rep_df_data = []
                            for line in report_lines:
                                if ":" in line:
                                    key_part, val_part = line.split(":", 1)
                                    val_clean = val_part.strip()
                                    # Assign a simple status emoji for quick scan
                                    if any(x in val_clean for x in ["✓", "Bullish", "High ✓", "Institutional", "Trending ✓", "Strong", "Above ✓", "Low"]):
                                        status = "🟢"
                                    elif any(x in val_clean for x in ["✗", "Bearish", "Low ✗", "Sideways", "Retail", "High Risk", "Below", "High ✗"]):
                                        status = "🔴"
                                    else:
                                        status = "🟡"
                                    rep_df_data.append({
                                        "": status,
                                        "Metric": key_part.strip(),
                                        "Value": val_clean,
                                    })
                            if rep_df_data:
                                rep_df = pd.DataFrame(rep_df_data)
                                # Plain render — no styler so it never breaks
                                st.dataframe(
                                    rep_df,
                                    use_container_width=True,
                                    hide_index=True,
                                    height=min(38 * len(rep_df_data) + 40, 560),
                                )
                            else:
                                # Fallback: show as formatted text
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

    if st.session_state.get("scan_errors"):
        with st.expander(f"⚠️ Skipped/failed symbols ({len(st.session_state['scan_errors'])})"): st.text("\n".join(st.session_state["scan_errors"][:20]))

# Pass your Fyers object here:
# show_scanner(fyers)
        # మెమరీ క్లీనప్ - ఇది యాప్ క్రాష్ అవ్వకుండా ఆపుతుంది[span_1](start_span)[span_1](end_span)
        del df
        gc.collect() 
