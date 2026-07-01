import streamlit as st
import pandas as pd
import numpy as np
import requests
import time
import io
from datetime import datetime, timedelta
from typing import List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

# XGBoost is optional — the app still works fully without it, the
# "XGBoost Trend / Confidence" columns just show "N/A" until it's installed.
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

# NOTE ON OPEN INTEREST: this scanner runs against the NSE_CM (cash
# equity) symbol master. Open Interest and Change in OI are futures &
# options concepts and do not exist for cash equity instruments, so they
# are intentionally NOT part of the scoring/columns below. Wiring OI in
# would require mapping every EQ symbol to its current-month FUT contract
# and pulling OI from a separate Fyers endpoint per symbol — that's a
# meaningfully different (and much heavier) scan and isn't included here.


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

    return sorted(set(symbols))


# ── Benchmark (NIFTY) fetch, used for Relative Strength ─────────────────────
@st.cache_data(ttl=60 * 30)
def fetch_nifty_benchmark(_fyers) -> Optional[pd.Series]:
    """Fetches NIFTY50 index daily closes for the same window as the scan.
    Cached separately from the per-symbol scan since every symbol shares it."""
    try:
        resp = _fyers.history({
            "symbol": NIFTY_BENCHMARK_SYMBOL, "resolution": "D", "date_format": "1",
            "range_from": DATE_FROM, "range_to": DATE_TO, "cont_flag": "1"
        })
        if not isinstance(resp, dict) or resp.get("s") != "ok":
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


def calculate_ai_trend(ai_score: float) -> Tuple[str, float]:
    if ai_score >= 65:
        return "📈 Bullish", round(ai_score, 1)
    if ai_score <= 40:
        return "📉 Bearish", round(100 - ai_score, 1)
    return "➖ Neutral", round(100 - abs(ai_score - 50) * 2, 1)


def calculate_xgboost_prediction(df: pd.DataFrame) -> Tuple[str, float]:
    """Trains a tiny per-symbol XGBoost classifier on engineered features to
    predict next-day direction. This is intentionally lightweight (50 trees,
    depth 3) since it re-trains from scratch for every symbol, every scan —
    it is NOT a persisted, backtested model. Gate this behind the UI
    checkbox for large scans; it adds real time per symbol."""
    if not XGBOOST_AVAILABLE or len(df) < 100:
        return "N/A", 0.0

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
    if len(d) < 60:
        return "N/A", 0.0

    train = d.iloc[:-1]
    latest = d.iloc[[-1]]
    X_train, y_train = train[feature_cols], train["Target"]
    if y_train.nunique() < 2:
        return "N/A", 0.0

    try:
        model = xgb.XGBClassifier(
            n_estimators=50, max_depth=3, learning_rate=0.1,
            eval_metric="logloss", verbosity=0,
        )
        model.fit(X_train, y_train)
        proba = model.predict_proba(latest[feature_cols])[0]
        pred = model.predict(latest[feature_cols])[0]
        confidence = round(float(max(proba)) * 100, 1)
        trend = "📈 Up" if pred == 1 else "📉 Down"
        return trend, confidence
    except Exception:
        return "N/A", 0.0


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
    """Composite score combining every requested input (minus OI, see note
    at top of file) into a 5-tier signal."""
    score = 0

    if ai_score > 70: score += 2
    elif ai_score > 55: score += 1
    elif ai_score < 30: score -= 2
    elif ai_score < 45: score -= 1

    if xgb_trend == "📈 Up": score += 1
    elif xgb_trend == "📉 Down": score -= 1

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


# ── Existing SMC / CISD logic (unchanged) ────────────────────────────────────
def _calculate_smc_and_cisd(df: pd.DataFrame):
    """Simplified Smart Money Concepts structure + CISD detection on daily candles.
    Returns (smc_structure, cisd_signal, signal_time_str)."""
    if len(df) < 30:
        return "Range ➖", "None", "N/A"

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
    cisd_time_str = "N/A"
    if not cisd_events.empty:
        is_bull = bool(cisd_events["Bullish_CISD"].iloc[-1])
        cisd_signal = "Bullish CISD 🚀" if is_bull else "Bearish CISD 🩸"
        cisd_time_str = cisd_events["Time"].iloc[-1].strftime("%d-%b-%Y")

    smc_events = recent[recent["Break_Up"] | recent["Break_Down"]]
    smc_structure = "Range ➖"
    smc_time_str = "N/A"
    if not smc_events.empty:
        is_up = bool(smc_events["Break_Up"].iloc[-1])
        is_bull_trend = bool(smc_events["Bullish_Trend"].iloc[-1])
        if is_up:
            smc_structure = "BOS 📈" if is_bull_trend else "CHOCH 🐂"
        else:
            smc_structure = "BOS 📉" if not is_bull_trend else "CHOCH 🐻"
        smc_time_str = smc_events["Time"].iloc[-1].strftime("%d-%b-%Y")

    signal_time = cisd_time_str if cisd_signal != "None" else (
        smc_time_str if smc_structure != "Range ➖" else df["Time"].iloc[-1].strftime("%d-%b-%Y")
    )

    return smc_structure, cisd_signal, signal_time


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

    # ── Gap % (today's open vs previous close) ─────────────────────────────
    gap_pct = 0.0
    if len(df) >= 2:
        gap_pct = ((df["Open"].iloc[-1] - df["Close"].iloc[-2]) / df["Close"].iloc[-2]) * 100
    gap_str = f"{gap_pct:.2f}%"
    if gap_pct >= 0.5:
        gap_str += " 🟢"
    elif gap_pct <= -0.5:
        gap_str += " 🔴"

    # ── SMC structure / CISD / signal time (existing, unchanged) ───────────
    smc_structure, cisd_signal, signal_time = _calculate_smc_and_cisd(df)

    # ── 52-week high/low status (DATE_FROM window ≈ 52 weeks) ──────────────
    h52w = df["High"].max()
    l52w = df["Low"].min()
    last_close = close.iloc[-1]
    if last_close >= h52w * 0.97:
        status_52w = "🟢 Near High"
    elif last_close <= l52w * 1.03:
        status_52w = "🔴 Near Low"
    else:
        status_52w = "Mid Range"

    # ── Breakout / Support / Resistance (vs prior 20-day high/low) ─────────
    resistance = df["High"].rolling(20).max().shift(1).iloc[-1]
    support = df["Low"].rolling(20).min().shift(1).iloc[-1]
    if pd.notna(resistance) and last_close > resistance:
        breakout = "📈 Bullish"
    elif pd.notna(support) and last_close < support:
        breakout = "📉 Bearish"
    else:
        breakout = "NO"

    # ── New indicators ───────────────────────────────────────────────────
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

    xgb_trend, xgb_confidence = ("N/A", 0.0)
    if enable_xgboost:
        xgb_trend, xgb_confidence = calculate_xgboost_prediction(df)

    alerts = generate_alerts(rvol, breakout, cisd_signal, mtf_trend, gap_pct)

    final_signal = calculate_final_signal(
        ai_score=ai_score, xgb_trend=xgb_trend, mtf_trend=mtf_trend, rs_label=rs_label,
        rsi=rsi_val, macd_bullish=macd_bullish, supertrend_bullish=supertrend_bullish,
        breakout=breakout, cisd_signal=cisd_signal, smc_structure=smc_structure,
    )

    stock_ticker = symbol.replace("NSE:", "").replace("-EQ", "")

    return {
        # ── Requested columns, in the exact requested order ────────────────
        "Signal Time": signal_time,
        "Stock": stock_ticker,
        "LTP": round(last_close, 2),
        "Gap %": gap_str,
        "Target": target,
        "Stoploss": stoploss,
        "SMC Structure": smc_structure,
        "CISD": cisd_signal,
        "XGBoost Trend": xgb_trend,
        "XGBoost Confidence (%)": xgb_confidence,
        "Alerts": alerts,
        "MTF Trend": mtf_trend,
        "AI Trend": ai_trend,
        "AI Confidence (%)": ai_confidence,
        "RS vs NIFTY": rs_label,
        "Support": round(float(support), 2) if pd.notna(support) else None,
        "Resistance": round(float(resistance), 2) if pd.notna(resistance) else None,
        "52W High": round(float(h52w), 2),
        "52W Low": round(float(l52w), 2),
        "52W Status": status_52w,
        "RSI": rsi_val,
        "Breakout Status": breakout,
        "MACD Signal": macd_signal_str,
        "Supertrend": supertrend_label,
        "VWAP": vwap_val,
        "Chart Pattern": chart_pattern,
        "RVOL": round(rvol, 2),
        "AI Score": ai_score,
        "Final Signal": final_signal,
        # ── Existing columns kept for backward compatibility (not removed) ─
        "Smart Money": "🏦 Institutional" if ai_score > 70 else "⚖️ Neutral" if ai_score > 45 else "🔻 Distribution",
        "Signal": "🟢 BUY" if ai_score > 65 else "🔴 SELL" if ai_score < 40 else "🟡 HOLD",
    }


def _fetch_symbol(fyers, symbol: str, nifty_close: Optional[pd.Series], enable_xgboost: bool):
    """Returns (result_dict_or_None, error_message_or_None)."""
    try:
        resp = fyers.history({
            "symbol": symbol, "resolution": "D", "date_format": "1",
            "range_from": DATE_FROM, "range_to": DATE_TO, "cont_flag": "1"
        })
    except Exception as e:
        return None, f"{symbol}: exception {e}"

    if not isinstance(resp, dict):
        return None, f"{symbol}: no response"
    if resp.get("s") != "ok":
        return None, f"{symbol}: {resp.get('message', resp.get('s'))}"
    candles = resp.get("candles")
    if not candles or len(candles) < 30:
        return None, f"{symbol}: insufficient history ({len(candles) if candles else 0} candles)"

    df = pd.DataFrame(candles, columns=["Time", "Open", "High", "Low", "Close", "Volume"])
    df["Time"] = pd.to_datetime(df["Time"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")

    try:
        return _analyse(symbol, df, nifty_close, enable_xgboost), None
    except Exception as e:
        return None, f"{symbol}: analysis error {e}"


def run_scan(fyers, symbols: List[str], nifty_close: Optional[pd.Series], enable_xgboost: bool):
    """Threaded, rate-limited scan with a progress bar. Returns (results, errors)."""
    results, errors = [], []
    progress = st.progress(0.0, text=f"Scanning 0 / {len(symbols)}")
    done = 0

    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i:i + BATCH_SIZE]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(_fetch_symbol, fyers, s, nifty_close, enable_xgboost): s for s in batch}
            for future in as_completed(futures):
                res, err = future.result()
                if res:
                    results.append(res)
                if err:
                    errors.append(err)
                done += 1
                progress.progress(done / len(symbols), text=f"Scanning {done} / {len(symbols)}")

        if i + BATCH_SIZE < len(symbols):
            time.sleep(BATCH_PAUSE_SECONDS)  # throttle between batches to respect rate limits

    progress.empty()
    return results, errors


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
        if any(x in val for x in ["🟡", "Wait", "HOLD", "Neutral", "Mixed", "Inline"]):
            return "color: #b8860b; font-weight: bold;"
    return ""


def _style_dataframe(df: pd.DataFrame):
    """pandas deprecated Styler.applymap in favor of .map (added in pandas
    2.1). Support both old and new pandas without erroring out."""
    styler = df.style
    if hasattr(styler, "map"):
        return styler.map(_color_code)
    return styler.applymap(_color_code)


def to_excel_bytes(df: pd.DataFrame) -> bytes:
    """Builds an in-memory formatted .xlsx from the scan results."""
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Scan Results")
        ws = writer.sheets["Scan Results"]

        from openpyxl.styles import Font, PatternFill, Alignment

        header_font = Font(bold=True, color="FFFFFF", name="Arial")
        header_fill = PatternFill("solid", start_color="1F2937")
        for cell in ws[1]:
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal="center")

        for col_cells in ws.columns:
            length = max((len(str(c.value)) for c in col_cells if c.value is not None), default=10)
            ws.column_dimensions[col_cells[0].column_letter].width = max(length + 2, 10)

        ws.freeze_panes = "A2"

    buf.seek(0)
    return buf.getvalue()


# ── Main Application ──────────────────────────────────────────────────────────
def show_scanner(fyers):
    st.title("🚀 NSE AI PRO V13 — Institutional Scanner")

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
            "Enable XGBoost predictions", value=False,
            help="Trains a small model per stock, per scan. Adds noticeable time — "
                 "recommended only with a limited symbol count." + (
                     "" if XGBOOST_AVAILABLE else " (xgboost package not installed — install with `pip install xgboost`)"
                 ),
            disabled=not XGBOOST_AVAILABLE,
        )
    with col3:
        st.caption(
            f"Estimated time at {MAX_WORKERS} concurrent workers: "
            f"~{((limit or len(symbols)) / MAX_WORKERS) * 0.3 / 60:.1f}–"
            f"{((limit or len(symbols)) / MAX_WORKERS) * 1.0 / 60:.1f} min (rough estimate, "
            f"longer with XGBoost enabled)."
        )

    scan_universe = symbols if limit == 0 else symbols[:limit]

    if st.button(f"🚀 Run Scan ({len(scan_universe)} symbols)"):
        with st.spinner("Fetching NIFTY benchmark for Relative Strength…"):
            nifty_close = fetch_nifty_benchmark(fyers)
        if nifty_close is None:
            st.info("Could not fetch NIFTY50 benchmark — 'RS vs NIFTY' will show N/A for this scan.")

        with st.spinner("Scanning…"):
            results, errors = run_scan(fyers, scan_universe, nifty_close, enable_xgboost)
            scan_df = pd.DataFrame(results)

        st.session_state["scan_df"] = scan_df
        st.session_state["scan_errors"] = errors

        if errors:
            st.warning(f"{len(errors)} of {len(scan_universe)} symbols failed or were skipped.")

    if "scan_df" in st.session_state:
        df = st.session_state["scan_df"]

        if df.empty:
            st.error("Scan returned no usable results. Expand the error log below.")
        else:
            sorted_df = df.sort_values("AI Score", ascending=False)
            st.dataframe(_style_dataframe(sorted_df), use_container_width=True, height=500)
            st.bar_chart(df.set_index("Stock")["AI Score"])

            st.download_button(
                "📥 Download as Excel",
                data=to_excel_bytes(sorted_df),
                file_name=f"nse_scan_{datetime.today().strftime('%Y%m%d_%H%M')}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )

        if st.session_state.get("scan_errors"):
            with st.expander(f"⚠️ Errors / skipped symbols ({len(st.session_state['scan_errors'])})"):
                st.text("\n".join(st.session_state["scan_errors"][:200]))
                if len(st.session_state["scan_errors"]) > 200:
                    st.caption(f"...and {len(st.session_state['scan_errors']) - 200} more.")


# Fyers ఆబ్జెక్ట్‌ను ఇక్కడ పాస్ చేయండి
# show_scanner(fyers)
