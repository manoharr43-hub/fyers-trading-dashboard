"""
ai_market_intelligence.py
==============================================================================
Standalone AI-powered "Market Intelligence" dashboard module for a FYERS
Streamlit trading dashboard.

DESIGN CONTRACT
--------------------------------------------------------------------------
This module is fully self-contained. It does NOT import, modify, or depend
on scanner.py, option_chain.py, app.py, or trading.py, and it never touches
any existing BUY/SELL signal logic. It exposes exactly one public function:

    show_ai_market_intelligence(fyers)

which can be safely called from market.py, e.g.:

    from ai_market_intelligence import show_ai_market_intelligence
    show_ai_market_intelligence(fyers)

Every external call (FYERS API, RSS feeds, NSE endpoints) is wrapped in
try/except so a single bad response never crashes the dashboard - failing
sections show a friendly warning while the rest of the app keeps working.

Dependencies: streamlit, pandas, numpy, requests, feedparser,
streamlit-autorefresh (optional - degrades gracefully if not installed).

Python: 3.11
==============================================================================
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import requests
import feedparser
import streamlit as st

try:
    from streamlit_autorefresh import st_autorefresh
    _HAS_AUTOREFRESH = True
except Exception:  # pragma: no cover - optional dependency
    _HAS_AUTOREFRESH = False


# ==============================================================================
# 1. CONFIGURATION & CONSTANTS
# ==============================================================================

REFRESH_INTERVAL_MS: int = 60_000  # Section 12: Auto Refresh every 60 seconds

INDEX_SYMBOLS: Dict[str, str] = {
    "NIFTY": "NSE:NIFTY50-INDEX",
    "BANKNIFTY": "NSE:NIFTYBANK-INDEX",
    "FINNIFTY": "NSE:FINNIFTY-INDEX",
    "MIDCPNIFTY": "NSE:MIDCPNIFTY-INDEX",
    "SENSEX": "BSE:SENSEX-INDEX",
    "INDIA VIX": "NSE:INDIAVIX-INDEX",
}

FUTURES_UNDERLYINGS: Dict[str, str] = {
    "NIFTY Futures": "NIFTY",
    "BANKNIFTY Futures": "BANKNIFTY",
    "FINNIFTY Futures": "FINNIFTY",
}

NEWS_FEEDS: Dict[str, str] = {
    "MoneyControl": "https://www.moneycontrol.com/rss/marketreports.xml",
    "Economic Times": "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "Business Standard": "https://www.business-standard.com/rss/markets-106.rss",
    "Reuters": "https://www.reutersagency.com/feed/?best-topics=markets&post_type=best",
    "Yahoo Finance": "https://finance.yahoo.com/news/rssindex",
}

WATCHLIST_DEFAULT: List[str] = [
    "NSE:RELIANCE-EQ",
    "NSE:TCS-EQ",
    "NSE:HDFCBANK-EQ",
    "NSE:INFY-EQ",
    "NSE:ICICIBANK-EQ",
]

BULLISH_KEYWORDS: List[str] = [
    "surge", "rally", "record high", "upgrade", "beat estimates", "strong growth",
    "bullish", "buy rating", "outperform", "gains", "rises", "jump", "soar",
    "upbeat", "positive outlook", "expansion", "profit rise", "inflow",
    "upgraded to buy", "all-time high", "recovery",
]

BEARISH_KEYWORDS: List[str] = [
    "crash", "plunge", "selloff", "sell-off", "downgrade", "miss estimates",
    "weak growth", "bearish", "sell rating", "underperform", "loss", "falls",
    "tumble", "slump", "negative outlook", "contraction", "profit fall",
    "outflow", "downgraded to sell", "recession", "default", "fraud",
]

HIGH_IMPACT_KEYWORDS: List[str] = [
    "rbi", "fed", "federal reserve", "war", "crash", "budget", "gdp",
    "inflation", "rate hike", "rate cut", "election", "geopolitical",
    "crisis", "recession", "default", "bankruptcy", "emergency",
]

MEDIUM_IMPACT_KEYWORDS: List[str] = [
    "earnings", "results", "ipo", "merger", "acquisition", "guidance",
    "policy", "tariff", "export", "import", "stake sale", "block deal",
]

KNOWN_STOCK_TOKENS: List[str] = [
    "RELIANCE", "TCS", "HDFC", "INFOSYS", "INFY", "ICICI", "SBI", "ITC",
    "L&T", "LT", "ADANI", "TATA", "WIPRO", "AXIS", "MARUTI", "BAJAJ",
    "KOTAK", "HCLTECH", "SUNPHARMA", "TITAN",
]


# ==============================================================================
# 2. GENERIC HELPERS / SAFE EXECUTION  (Section 14: Error Handling)
# ==============================================================================

def _safe_call(fn, *args: Any, default: Any = None, **kwargs: Any) -> Any:
    """Execute fn(*args, **kwargs) and swallow all exceptions, returning `default` on failure."""
    try:
        return fn(*args, **kwargs)
    except Exception:
        return default


def _render_safely(section_name: str, fn, *args: Any, **kwargs: Any) -> None:
    """Render a dashboard section without ever letting it crash the whole app."""
    try:
        fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - intentional catch-all for UI resilience
        st.error(f"⚠️ The '{section_name}' section could not be loaded ({exc}). Other sections are unaffected.")


def _sentiment_color(label: str) -> str:
    return {"Bullish": "🟢", "Bearish": "🔴", "Neutral": "🟡"}.get(label, "⚪")


def _trend_color(trend: str) -> str:
    return {"UP": "🟢", "DOWN": "🔴", "FLAT": "🟡", "NA": "⚪"}.get(trend, "⚪")


# ==============================================================================
# 3. TECHNICAL INDICATORS (independent implementation - EMA, RSI, MACD, ADX,
#    ATR, VWAP, Support/Resistance)
# ==============================================================================

def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential Moving Average."""
    return series.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index (Wilder smoothing)."""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi_val = 100 - (100 / (1 + rs))
    return rsi_val.fillna(50.0)


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """MACD line, signal line, and histogram."""
    ema_fast = ema(series, fast)
    ema_slow = ema(series, slow)
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def _true_range(df: pd.DataFrame) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    return pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range."""
    tr = _true_range(df)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average Directional Index."""
    high, low = df["high"], df["low"]
    tr = _true_range(df)
    atr_val = tr.ewm(alpha=1 / period, adjust=False).mean()

    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / period, adjust=False).mean() / atr_val.replace(0, np.nan)
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / period, adjust=False).mean() / atr_val.replace(0, np.nan)

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / period, adjust=False).mean().fillna(0.0)


def vwap(df: pd.DataFrame) -> pd.Series:
    """Volume Weighted Average Price (session cumulative)."""
    typical_price = (df["high"] + df["low"] + df["close"]) / 3
    cum_vol = df["volume"].cumsum().replace(0, np.nan)
    cum_tp_vol = (typical_price * df["volume"]).cumsum()
    return (cum_tp_vol / cum_vol).bfill().ffill()


def support_resistance(df: pd.DataFrame, window: int = 20) -> Tuple[float, float]:
    """Simple rolling-window support/resistance."""
    recent = df.tail(window)
    return float(recent["low"].min()), float(recent["high"].max())


# ==============================================================================
# 4. FYERS DATA ACCESS LAYER (quotes, history, option chain)
# ==============================================================================

@st.cache_data(ttl=15, show_spinner=False)
def fetch_quote(_fyers: Any, symbol: str) -> Optional[Dict[str, Any]]:
    """Fetch a single live quote from FYERS. Returns None on any failure."""
    try:
        resp = _fyers.quotes({"symbols": symbol})
        if resp and resp.get("s") == "ok" and resp.get("d"):
            return resp["d"][0].get("v")
    except Exception:
        return None
    return None


def fetch_history(_fyers: Any, symbol: str, resolution: str = "15", days: int = 30) -> Optional[pd.DataFrame]:
    """Fetch OHLCV candles from FYERS history API. Returns None on any failure."""
    try:
        to_date = dt.date.today()
        from_date = to_date - dt.timedelta(days=days)
        payload = {
            "symbol": symbol,
            "resolution": resolution,
            "date_format": "1",
            "range_from": from_date.strftime("%Y-%m-%d"),
            "range_to": to_date.strftime("%Y-%m-%d"),
            "cont_flag": "1",
        }
        resp = _fyers.history(payload)
        if resp and resp.get("s") == "ok" and resp.get("candles"):
            df = pd.DataFrame(resp["candles"], columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s")
            return df
    except Exception:
        return None
    return None


@st.cache_data(ttl=120, show_spinner=False)
def fetch_history_cached(_fyers: Any, symbol: str, resolution: str, days: int) -> Optional[pd.DataFrame]:
    return fetch_history(_fyers, symbol, resolution, days)


@st.cache_data(ttl=60, show_spinner=False)
def fetch_option_chain_metrics(_fyers: Any, symbol: str) -> Tuple[Optional[float], Optional[int], Optional[str], Optional[str]]:
    """Return (PCR, total OI, put-writing status, call-writing status). None on failure."""
    try:
        resp = _fyers.optionchain({"symbol": symbol, "strikecount": 10, "timestamp": ""})
        if resp and resp.get("s") == "ok":
            option_chain = resp.get("data", {}).get("optionsChain", [])
            put_oi = sum(o.get("oi", 0) for o in option_chain if o.get("option_type") == "PE")
            call_oi = sum(o.get("oi", 0) for o in option_chain if o.get("option_type") == "CE")
            total_oi = put_oi + call_oi
            pcr = round(put_oi / call_oi, 2) if call_oi else None
            put_writing = "Active" if put_oi > call_oi else "Weak"
            call_writing = "Active" if call_oi > put_oi else "Weak"
            return pcr, total_oi, put_writing, call_writing
    except Exception:
        return None, None, None, None
    return None, None, None, None


@st.cache_data(ttl=30, show_spinner=False)
def get_market_summary_cached(_fyers: Any, symbols_tuple: Tuple[Tuple[str, str], ...]) -> pd.DataFrame:
    """Build the live market-summary table (Section 2)."""
    rows: List[Dict[str, Any]] = []
    for name, sym in symbols_tuple:
        q = fetch_quote(_fyers, sym)
        if q:
            ltp = q.get("lp", np.nan)
            prev_close = q.get("prev_close_price", ltp)
            change = q.get("ch", (ltp - prev_close) if prev_close else 0.0)
            change_pct = q.get("chp", (change / prev_close * 100) if prev_close else 0.0)
            rows.append({
                "Index": name,
                "LTP": ltp,
                "Change": change,
                "Change %": change_pct,
                "High": q.get("high_price", np.nan),
                "Low": q.get("low_price", np.nan),
                "Open": q.get("open_price", np.nan),
                "Prev Close": prev_close,
                "Volume": q.get("volume", 0),
                "Trend": "UP" if change > 0 else ("DOWN" if change < 0 else "FLAT"),
            })
        else:
            rows.append({
                "Index": name, "LTP": np.nan, "Change": np.nan, "Change %": np.nan,
                "High": np.nan, "Low": np.nan, "Open": np.nan, "Prev Close": np.nan,
                "Volume": 0, "Trend": "NA",
            })
    return pd.DataFrame(rows)


def _current_month_future_symbol(underlying: str) -> str:
    """Best-effort construction of the near-month futures symbol.
    NOTE: adjust the naming convention here if your FYERS contract master differs."""
    today = dt.date.today()
    exch = "BSE" if underlying == "SENSEX" else "NSE"
    month_code = today.strftime("%b").upper()
    yy = today.strftime("%y")
    return f"{exch}:{underlying}{yy}{month_code}FUT"


# ==============================================================================
# 5. SMART MONEY CONCEPTS (SMC) ENGINE
# ==============================================================================

def _find_swings(df: pd.DataFrame, left: int = 3, right: int = 3) -> Tuple[List[int], List[int]]:
    """Fractal-style swing high/low detection."""
    highs, lows = [], []
    h, l = df["high"].values, df["low"].values
    n = len(df)
    for i in range(left, n - right):
        window_h = h[i - left:i + right + 1]
        window_l = l[i - left:i + right + 1]
        if h[i] == window_h.max():
            highs.append(i)
        if l[i] == window_l.min():
            lows.append(i)
    return highs, lows


def compute_smc(df: pd.DataFrame) -> Dict[str, Any]:
    """Compute a snapshot of Smart Money Concepts for the latest candles (Section 10)."""
    result: Dict[str, Any] = {
        "swing_high": None, "swing_low": None, "bos": "NONE", "choch": "NONE",
        "order_block": "NONE", "breaker_block": "NONE", "mitigation_block": "NONE",
        "fvg": "NONE", "liquidity_sweep": "NONE", "liquidity_pool": "NONE",
        "equal_high": False, "equal_low": False, "cisd": "NONE", "zone": "NEUTRAL",
    }
    try:
        if df is None or len(df) < 20:
            return result

        highs_idx, lows_idx = _find_swings(df)
        if not highs_idx or not lows_idx:
            return result

        last_swing_high = float(df["high"].iloc[highs_idx[-1]])
        last_swing_low = float(df["low"].iloc[lows_idx[-1]])
        result["swing_high"] = last_swing_high
        result["swing_low"] = last_swing_low

        close = float(df["close"].iloc[-1])
        prev_trend_up = df["close"].iloc[-1] > df["close"].iloc[-10] if len(df) >= 10 else True

        # Break of Structure / Change of Character
        if close > last_swing_high:
            result["bos"] = "BULLISH BOS" if prev_trend_up else "NONE"
            result["choch"] = "BULLISH CHOCH" if not prev_trend_up else "NONE"
        elif close < last_swing_low:
            result["bos"] = "BEARISH BOS" if not prev_trend_up else "NONE"
            result["choch"] = "BEARISH CHOCH" if prev_trend_up else "NONE"

        # Order Block: last opposite candle preceding the break
        if close > last_swing_high:
            down_candles = df[df["close"] < df["open"]].tail(1)
            if not down_candles.empty:
                result["order_block"] = f"Bullish OB @ {down_candles['low'].iloc[-1]:.2f}"
        elif close < last_swing_low:
            up_candles = df[df["close"] > df["open"]].tail(1)
            if not up_candles.empty:
                result["order_block"] = f"Bearish OB @ {up_candles['high'].iloc[-1]:.2f}"

        # Fair Value Gap (3-candle imbalance) on the most recent candles
        if len(df) >= 3:
            c1, c3 = df.iloc[-3], df.iloc[-1]
            if c1["high"] < c3["low"]:
                result["fvg"] = f"Bullish FVG {c1['high']:.2f}-{c3['low']:.2f}"
            elif c1["low"] > c3["high"]:
                result["fvg"] = f"Bearish FVG {c3['high']:.2f}-{c1['low']:.2f}"

        # Liquidity sweep: wick beyond a swing point followed by a close back inside
        last_candle = df.iloc[-1]
        if last_candle["high"] > last_swing_high and last_candle["close"] < last_swing_high:
            result["liquidity_sweep"] = "Sell-side sweep of swing high"
        elif last_candle["low"] < last_swing_low and last_candle["close"] > last_swing_low:
            result["liquidity_sweep"] = "Buy-side sweep of swing low"

        # Equal highs / equal lows (tolerance ~0.15%)
        if len(highs_idx) >= 2:
            h1, h2 = df["high"].iloc[highs_idx[-1]], df["high"].iloc[highs_idx[-2]]
            if h2 and abs(h1 - h2) / h2 < 0.0015:
                result["equal_high"] = True
        if len(lows_idx) >= 2:
            l1, l2 = df["low"].iloc[lows_idx[-1]], df["low"].iloc[lows_idx[-2]]
            if l2 and abs(l1 - l2) / l2 < 0.0015:
                result["equal_low"] = True
        if result["equal_high"] or result["equal_low"]:
            result["liquidity_pool"] = "Equal highs/lows liquidity pool detected"

        # Breaker / Mitigation block (simplified heuristic)
        if result["bos"] != "NONE" and result["order_block"] != "NONE":
            result["breaker_block"] = "Potential breaker block at former order block"
            result["mitigation_block"] = "Mitigation zone active near order block"

        # CISD (Change in State of Delivery) - simplified confirmation marker
        if result["liquidity_sweep"] != "NONE":
            bullish_confirm = (last_candle["close"] - last_candle["open"]) > 0
            expects_bullish = "Buy" in result["liquidity_sweep"]
            result["cisd"] = "CISD confirmed" if bullish_confirm == expects_bullish else "Pending"

        # Premium / Discount zone (50% of swing range)
        rng = last_swing_high - last_swing_low
        if rng > 0:
            midpoint = last_swing_low + rng * 0.5
            result["zone"] = "PREMIUM" if close > midpoint else "DISCOUNT"
    except Exception:
        pass
    return result


# ==============================================================================
# 6. NEWS ENGINE (fetch + sentiment + impact classification)
# ==============================================================================

def _classify_sentiment(text: str) -> Tuple[str, float]:
    text_l = text.lower()
    bull_score = sum(1 for kw in BULLISH_KEYWORDS if kw in text_l)
    bear_score = sum(1 for kw in BEARISH_KEYWORDS if kw in text_l)
    if bull_score == 0 and bear_score == 0:
        return "Neutral", 50.0
    if bull_score > bear_score:
        return "Bullish", round(min(95.0, 55 + (bull_score - bear_score) * 8), 1)
    if bear_score > bull_score:
        return "Bearish", round(min(95.0, 55 + (bear_score - bull_score) * 8), 1)
    return "Neutral", 50.0


def _classify_impact(text: str) -> str:
    text_l = text.lower()
    if any(kw in text_l for kw in HIGH_IMPACT_KEYWORDS):
        return "High"
    if any(kw in text_l for kw in MEDIUM_IMPACT_KEYWORDS):
        return "Medium"
    return "Low"


def _affected_assets(text: str) -> Tuple[str, List[str]]:
    text_u = text.upper()
    affected_index = "NONE"
    for idx_name in INDEX_SYMBOLS:
        if idx_name in text_u:
            affected_index = idx_name
            break
    stocks = [tok for tok in KNOWN_STOCK_TOKENS if tok in text_u]
    return affected_index, stocks


@st.cache_data(ttl=180, show_spinner=False)
def fetch_all_news(max_per_source: int = 6) -> pd.DataFrame:
    """Download and analyze the latest news headlines (Sections 4 & 5)."""
    rows: List[Dict[str, Any]] = []
    for source, url in NEWS_FEEDS.items():
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:max_per_source]:
                title = getattr(entry, "title", "").strip()
                if not title:
                    continue
                published = getattr(entry, "published", "") or getattr(entry, "updated", "") or ""
                sentiment, confidence = _classify_sentiment(title)
                impact = _classify_impact(title)
                affected_index, affected_stocks = _affected_assets(title)
                rows.append({
                    "Time": published,
                    "Headline": title,
                    "Category": "Markets",
                    "Source": source,
                    "Sentiment": sentiment,
                    "Confidence": confidence,
                    "Impact": impact,
                    "Affected Index": affected_index,
                    "Affected Stocks": ", ".join(affected_stocks) if affected_stocks else "NONE",
                })
        except Exception:
            continue
    columns = ["Time", "Headline", "Category", "Source", "Sentiment", "Confidence",
               "Impact", "Affected Index", "Affected Stocks"]
    return pd.DataFrame(rows, columns=columns) if rows else pd.DataFrame(columns=columns)


# ==============================================================================
# 7. FII / DII ENGINE
# ==============================================================================

@st.cache_data(ttl=300, show_spinner=False)
def fetch_fii_dii() -> Optional[Dict[str, float]]:
    """Attempt to auto-fetch FII/DII activity. Returns None if unavailable (manual entry fallback)."""
    try:
        headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
        session = requests.Session()
        session.get("https://www.nseindia.com", headers=headers, timeout=5)
        resp = session.get("https://www.nseindia.com/api/fiidiiTradeReact", headers=headers, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            fii = next((d for d in data if "FII" in str(d.get("category", ""))), None)
            dii = next((d for d in data if "DII" in str(d.get("category", ""))), None)
            if fii and dii:
                return {
                    "fii_buy": float(fii.get("buyValue", 0)),
                    "fii_sell": float(fii.get("sellValue", 0)),
                    "dii_buy": float(dii.get("buyValue", 0)),
                    "dii_sell": float(dii.get("sellValue", 0)),
                }
    except Exception:
        return None
    return None


# ==============================================================================
# 8. FUTURES ANALYSIS ENGINE
# ==============================================================================

def classify_futures_buildup(price_change_pct: float, oi_change_pct: float) -> str:
    if price_change_pct > 0 and oi_change_pct > 0:
        return "Long Build Up"
    if price_change_pct < 0 and oi_change_pct > 0:
        return "Short Build Up"
    if price_change_pct > 0 and oi_change_pct < 0:
        return "Short Covering"
    if price_change_pct < 0 and oi_change_pct < 0:
        return "Long Unwinding"
    return "Neutral"


# ==============================================================================
# 9. AI PREDICTION ENGINE
# ==============================================================================

@dataclass
class PredictionResult:
    recommendation: str
    confidence: float
    target1: float
    target2: float
    stoploss: float
    risk_reward: float
    score: float


def generate_ai_prediction(
    ltp: float,
    trend_score: float,
    news_score: float,
    fii_dii_score: float,
    rsi_val: float,
    macd_hist: float,
    vwap_val: float,
    adx_val: float,
    smc_score: float,
    volume_score: float,
    institutional_score: float,
    atr_val: float,
) -> PredictionResult:
    """Combine multiple weighted signals into a single AI trade recommendation (Section 11)."""
    try:
        weights = {
            "trend": 0.18, "news": 0.10, "fii_dii": 0.10, "rsi": 0.10,
            "macd": 0.12, "vwap": 0.08, "adx": 0.08, "smc": 0.14,
            "volume": 0.05, "institutional": 0.05,
        }
        rsi_score = (rsi_val - 50) / 50 * 100
        macd_score = max(-100.0, min(100.0, macd_hist * 1000))
        vwap_score = 100.0 if ltp > vwap_val else -100.0
        adx_score = min(adx_val, 100.0) * (1 if trend_score >= 0 else -1)

        composite = (
            trend_score * weights["trend"]
            + news_score * weights["news"]
            + fii_dii_score * weights["fii_dii"]
            + rsi_score * weights["rsi"]
            + macd_score * weights["macd"]
            + vwap_score * weights["vwap"]
            + adx_score * weights["adx"]
            + smc_score * weights["smc"]
            + volume_score * weights["volume"]
            + institutional_score * weights["institutional"]
        )
        composite = max(-100.0, min(100.0, composite))
        confidence = round(min(97.0, 50 + abs(composite) * 0.45), 1)

        if composite >= 60:
            rec = "STRONG BUY"
        elif composite >= 20:
            rec = "BUY"
        elif composite > -20:
            rec = "WATCH"
        elif composite > -60:
            rec = "SELL"
        else:
            rec = "STRONG SELL"

        atr_val = atr_val if atr_val and atr_val > 0 else ltp * 0.005
        direction = 1 if composite >= 0 else -1
        target1 = round(ltp + direction * atr_val * 1.5, 2)
        target2 = round(ltp + direction * atr_val * 3.0, 2)
        stoploss = round(ltp - direction * atr_val * 1.0, 2)
        risk = abs(ltp - stoploss)
        reward = abs(target1 - ltp)
        rr = round(reward / risk, 2) if risk > 0 else 0.0

        return PredictionResult(rec, confidence, target1, target2, stoploss, rr, round(composite, 2))
    except Exception:
        return PredictionResult("WATCH", 50.0, ltp, ltp, ltp, 0.0, 0.0)


# ==============================================================================
# MARKET SCORE / VIX CLASSIFICATION (used by the AI Dashboard & VIX sections)
# ==============================================================================

def compute_overall_market_score(market_df: pd.DataFrame, vix_value: Optional[float], news_df: pd.DataFrame) -> Dict[str, Any]:
    """Aggregate market breadth, news sentiment, and VIX into an overall AI score (Section 1)."""
    try:
        changes = market_df["Change %"].dropna() if not market_df.empty else pd.Series(dtype=float)
        bullish = int((changes > 0).sum())
        bearish = int((changes < 0).sum())
        neutral = int((changes == 0).sum()) + (int(market_df["Change %"].isna().sum()) if not market_df.empty else 0)

        avg_change = float(changes.mean()) if not changes.empty else 0.0
        trend_component = max(-100.0, min(100.0, avg_change * 20))

        if not news_df.empty and "Sentiment" in news_df.columns:
            bull_news = int((news_df["Sentiment"] == "Bullish").sum())
            bear_news = int((news_df["Sentiment"] == "Bearish").sum())
            total_news = max(1, len(news_df))
            news_component = ((bull_news - bear_news) / total_news) * 100
        else:
            news_component = 0.0

        vix_for_calc = vix_value if vix_value else 15.0
        vix_component = max(-100.0, min(100.0, (20 - vix_for_calc) * 5))

        overall_score = round(trend_component * 0.45 + news_component * 0.30 + vix_component * 0.25, 1)
        overall_score = max(-100.0, min(100.0, overall_score))

        market_health = round(50 + overall_score / 2, 1)
        risk_pct = round(max(5.0, min(95.0, vix_for_calc * 3)), 1)
        opportunity_pct = round(max(5.0, min(95.0, 50 + overall_score / 2)), 1)
        ai_confidence = round(max(30.0, min(97.0, 60 + abs(overall_score) * 0.35)), 1)

        if overall_score >= 25:
            status = "BULLISH"
        elif overall_score <= -25:
            status = "BEARISH"
        else:
            status = "NEUTRAL"

        return {
            "status": status, "bullish": bullish, "bearish": bearish, "neutral": neutral,
            "overall_score": overall_score, "market_health": market_health,
            "risk_pct": risk_pct, "opportunity_pct": opportunity_pct, "ai_confidence": ai_confidence,
        }
    except Exception:
        return {
            "status": "NEUTRAL", "bullish": 0, "bearish": 0, "neutral": 0,
            "overall_score": 0.0, "market_health": 50.0, "risk_pct": 50.0,
            "opportunity_pct": 50.0, "ai_confidence": 50.0,
        }


def classify_vix(vix_value: float) -> Dict[str, Any]:
    """Classify India VIX regime and suggest risk / position sizing (Section 3)."""
    try:
        if vix_value < 12:
            regime, risk, confidence, size = "Low Volatility", 20, 85, "Large (75-100% of normal size)"
        elif vix_value < 16:
            regime, risk, confidence, size = "Normal", 35, 78, "Normal (100% of standard size)"
        elif vix_value < 22:
            regime, risk, confidence, size = "High Volatility", 60, 65, "Reduced (50-60% of normal size)"
        else:
            regime, risk, confidence, size = "Very High", 85, 50, "Minimal (20-30% of normal size)"
        return {"regime": regime, "risk_pct": risk, "confidence_pct": confidence, "position_size": size}
    except Exception:
        return {"regime": "Unknown", "risk_pct": 50, "confidence_pct": 50, "position_size": "Normal"}


# ==============================================================================
# 10. UI RENDER FUNCTIONS
# ==============================================================================

def render_ai_dashboard(score: Dict[str, Any]) -> None:
    """Section 1: AI Dashboard."""
    st.subheader("🧠 AI Dashboard")
    status_emoji = {"BULLISH": "🟢", "BEARISH": "🔴", "NEUTRAL": "🟡"}.get(score["status"], "⚪")
    st.markdown(f"### Market Status: {status_emoji} **{score['status']}**")

    c1, c2, c3 = st.columns(3)
    c1.metric("Bullish Signals", score["bullish"])
    c2.metric("Bearish Signals", score["bearish"])
    c3.metric("Neutral Signals", score["neutral"])

    c4, c5 = st.columns(2)
    with c4:
        st.metric("Overall AI Score", f"{score['overall_score']}")
        st.progress(min(1.0, max(0.0, (score["overall_score"] + 100) / 200)))
    with c5:
        st.metric("Market Health %", f"{score['market_health']}%")
        st.progress(min(1.0, max(0.0, score["market_health"] / 100)))

    c6, c7, c8 = st.columns(3)
    with c6:
        st.metric("Risk %", f"{score['risk_pct']}%")
        st.progress(min(1.0, max(0.0, score["risk_pct"] / 100)))
    with c7:
        st.metric("Opportunity %", f"{score['opportunity_pct']}%")
        st.progress(min(1.0, max(0.0, score["opportunity_pct"] / 100)))
    with c8:
        st.metric("AI Confidence %", f"{score['ai_confidence']}%")
        st.progress(min(1.0, max(0.0, score["ai_confidence"] / 100)))


def render_market_summary(market_df: pd.DataFrame) -> None:
    """Section 2: Market Summary."""
    st.subheader("📊 Market Summary (Live)")
    if market_df.empty:
        st.warning("Market data unavailable right now.")
        return
    for _, row in market_df.iterrows():
        color = _trend_color(row.get("Trend", "NA"))
        with st.container():
            cols = st.columns([1.4, 1, 1, 1, 1, 1, 1, 1, 1])
            cols[0].markdown(f"**{color} {row['Index']}**")
            cols[1].metric("LTP", f"{row['LTP']:.2f}" if pd.notna(row["LTP"]) else "NA")
            cols[2].metric("Change", f"{row['Change']:.2f}" if pd.notna(row["Change"]) else "NA")
            cols[3].metric("Change %", f"{row['Change %']:.2f}%" if pd.notna(row["Change %"]) else "NA")
            cols[4].write(f"High: {row['High']:.2f}" if pd.notna(row["High"]) else "High: NA")
            cols[5].write(f"Low: {row['Low']:.2f}" if pd.notna(row["Low"]) else "Low: NA")
            cols[6].write(f"Open: {row['Open']:.2f}" if pd.notna(row["Open"]) else "Open: NA")
            cols[7].write(f"Prev: {row['Prev Close']:.2f}" if pd.notna(row["Prev Close"]) else "Prev: NA")
            cols[8].write(f"Vol: {int(row['Volume']):,}" if pd.notna(row["Volume"]) else "Vol: NA")
        st.divider()


def render_vix_analysis(vix_value: Optional[float]) -> None:
    """Section 3: India VIX Analysis."""
    st.subheader("📈 India VIX Analysis")
    if vix_value is None:
        st.warning("VIX data unavailable.")
        return
    info = classify_vix(vix_value)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("India VIX", f"{vix_value:.2f}")
    c2.metric("Regime", info["regime"])
    c3.metric("Risk %", f"{info['risk_pct']}%")
    c4.metric("Confidence %", f"{info['confidence_pct']}%")
    st.info(f"**Suggested Position Size:** {info['position_size']}")


def render_live_news(news_df: pd.DataFrame) -> None:
    """Section 4: Live News."""
    st.subheader("📰 Live News")
    if news_df.empty:
        st.warning("No live news available right now.")
        return
    display_df = news_df[["Time", "Headline", "Category", "Source", "Sentiment", "Confidence"]].copy()
    display_df["Sentiment"] = display_df["Sentiment"].apply(lambda s: f"{_sentiment_color(s)} {s}")
    st.dataframe(display_df, use_container_width=True, hide_index=True)


def render_ai_news_analysis(news_df: pd.DataFrame) -> None:
    """Section 5: AI News Analysis."""
    st.subheader("🤖 AI News Analysis")
    if news_df.empty:
        st.warning("No news to analyze.")
        return
    c1, c2, c3 = st.columns(3)
    c1.metric("Bullish", int((news_df["Sentiment"] == "Bullish").sum()))
    c2.metric("Bearish", int((news_df["Sentiment"] == "Bearish").sum()))
    c3.metric("Neutral", int((news_df["Sentiment"] == "Neutral").sum()))

    c4, c5, c6 = st.columns(3)
    c4.metric("High Impact", int((news_df["Impact"] == "High").sum()))
    c5.metric("Medium Impact", int((news_df["Impact"] == "Medium").sum()))
    c6.metric("Low Impact", int((news_df["Impact"] == "Low").sum()))

    with st.expander("Detailed Market Impact Analysis"):
        detail_df = news_df[["Headline", "Sentiment", "Impact", "Affected Index", "Affected Stocks"]].rename(
            columns={"Impact": "Market Impact"}
        )
        st.dataframe(detail_df, use_container_width=True, hide_index=True)


def render_fii_dii() -> Dict[str, float]:
    """Section 6: FII / DII. Returns the effective data dict (live or manual)."""
    st.subheader("🏦 FII / DII Activity")
    data = fetch_fii_dii()
    if data is None:
        st.warning("Live FII/DII data unavailable. Please enter today's figures manually.")
        if "manual_fii_dii" not in st.session_state:
            st.session_state.manual_fii_dii = {"fii_buy": 0.0, "fii_sell": 0.0, "dii_buy": 0.0, "dii_sell": 0.0}
        with st.form("manual_fii_dii_form"):
            c1, c2 = st.columns(2)
            fii_buy = c1.number_input("FII Buy (₹ Cr)", value=st.session_state.manual_fii_dii["fii_buy"])
            fii_sell = c2.number_input("FII Sell (₹ Cr)", value=st.session_state.manual_fii_dii["fii_sell"])
            dii_buy = c1.number_input("DII Buy (₹ Cr)", value=st.session_state.manual_fii_dii["dii_buy"])
            dii_sell = c2.number_input("DII Sell (₹ Cr)", value=st.session_state.manual_fii_dii["dii_sell"])
            if st.form_submit_button("Save"):
                st.session_state.manual_fii_dii = {
                    "fii_buy": fii_buy, "fii_sell": fii_sell, "dii_buy": dii_buy, "dii_sell": dii_sell,
                }
                st.success("Manual FII/DII data saved.")
        data = st.session_state.manual_fii_dii

    net_fii = data["fii_buy"] - data["fii_sell"]
    net_dii = data["dii_buy"] - data["dii_sell"]
    c1, c2, c3 = st.columns(3)
    with c1:
        st.metric("FII Buy", f"₹{data['fii_buy']:.1f} Cr")
        st.metric("FII Sell", f"₹{data['fii_sell']:.1f} Cr")
    with c2:
        st.metric("Net FII", f"₹{net_fii:.1f} Cr")
    with c3:
        st.metric("DII Buy", f"₹{data['dii_buy']:.1f} Cr")
        st.metric("DII Sell", f"₹{data['dii_sell']:.1f} Cr")
    st.metric("Net DII", f"₹{net_dii:.1f} Cr")
    return data


def render_index_analysis(fyers: Any) -> None:
    """Section 7: Index Analysis."""
    st.subheader("📐 Index Analysis")
    for name, symbol in INDEX_SYMBOLS.items():
        if name == "INDIA VIX":
            continue
        with st.expander(f"{name} — Technical Analysis", expanded=False):
            df = fetch_history_cached(fyers, symbol, "15", 30)
            if df is None or df.empty:
                st.warning("Historical data unavailable for this index.")
                continue
            try:
                e20 = ema(df["close"], 20).iloc[-1]
                e50 = ema(df["close"], 50).iloc[-1]
                e200 = ema(df["close"], 200).iloc[-1] if len(df) >= 200 else np.nan
                vwap_val = vwap(df).iloc[-1]
                rsi_val = rsi(df["close"]).iloc[-1]
                _, _, hist = macd(df["close"])
                adx_val = adx(df).iloc[-1]
                atr_val = atr(df).iloc[-1]
                support, resistance = support_resistance(df)
                close = df["close"].iloc[-1]
                trend = "UP" if close > e50 else "DOWN"

                pcr, oi_total, put_writing, call_writing = fetch_option_chain_metrics(fyers, symbol)
                prob_up = round(max(5.0, min(95.0, 50 + (rsi_val - 50) * 0.6 + hist.iloc[-1] * 100)), 1)
                prob_down = round(100 - prob_up, 1)

                c1, c2, c3, c4 = st.columns(4)
                c1.metric("Trend", trend)
                c2.metric("EMA20", f"{e20:.2f}")
                c3.metric("EMA50", f"{e50:.2f}")
                c4.metric("EMA200", f"{e200:.2f}" if not np.isnan(e200) else "NA")

                c5, c6, c7, c8 = st.columns(4)
                c5.metric("VWAP", f"{vwap_val:.2f}")
                c6.metric("RSI", f"{rsi_val:.1f}")
                c7.metric("MACD Hist", f"{hist.iloc[-1]:.3f}")
                c8.metric("ADX", f"{adx_val:.1f}")

                c9, c10, c11, c12 = st.columns(4)
                c9.metric("ATR", f"{atr_val:.2f}")
                c10.metric("Support", f"{support:.2f}")
                c11.metric("Resistance", f"{resistance:.2f}")
                c12.metric("PCR", f"{pcr:.2f}" if pcr is not None else "NA")

                c13, c14, c15, c16 = st.columns(4)
                c13.metric("Total OI", f"{oi_total:,}" if oi_total is not None else "NA")
                c14.metric("Put Writing", put_writing or "NA")
                c15.metric("Prob Up %", f"{prob_up}%")
                c16.metric("Prob Down %", f"{prob_down}%")
                st.caption(f"Call Writing: {call_writing or 'NA'}")
            except Exception as exc:
                st.warning(f"Unable to complete technical analysis for {name} ({exc}).")


def render_futures_analysis(fyers: Any) -> None:
    """Section 8: Futures Analysis."""
    st.subheader("📉 Futures Analysis")
    for label, underlying in FUTURES_UNDERLYINGS.items():
        with st.expander(label, expanded=False):
            try:
                fut_symbol = _current_month_future_symbol(underlying)
                q = fetch_quote(fyers, fut_symbol)
                if not q:
                    st.warning(f"Futures data unavailable for {label} ({fut_symbol}).")
                    continue
                price_change_pct = q.get("chp", 0.0)
                oi = q.get("oi")
                oi_change_pct = q.get("oipercent", 0.0)
                volume = q.get("volume", 0)
                buildup = classify_futures_buildup(price_change_pct, oi_change_pct)
                strength = min(100.0, abs(price_change_pct) * 10 + abs(oi_change_pct) * 5)

                if buildup in ("Long Build Up", "Short Covering"):
                    signal = "BUY"
                elif buildup in ("Short Build Up", "Long Unwinding"):
                    signal = "SELL"
                else:
                    signal = "WAIT"

                c1, c2, c3 = st.columns(3)
                c1.metric("Build Up", buildup)
                c2.metric("Trend Strength", f"{strength:.1f}%")
                c3.metric("AI Signal", signal)

                c4, c5, c6 = st.columns(3)
                c4.metric("Volume", f"{volume:,}" if volume else "NA")
                c5.metric("OI", f"{oi:,}" if oi else "NA")
                c6.metric("Price %", f"{price_change_pct:.2f}%")
            except Exception as exc:
                st.warning(f"Unable to analyze futures for {label} ({exc}).")


def render_stock_analysis(fyers: Any) -> None:
    """Section 9: Stock Analysis (watchlist)."""
    st.subheader("📌 Stock Analysis (Watchlist)")
    if "ai_watchlist" not in st.session_state:
        st.session_state.ai_watchlist = list(WATCHLIST_DEFAULT)

    c1, c2 = st.columns([3, 1])
    new_symbol = c1.text_input("Add symbol (e.g. NSE:TATAMOTORS-EQ)", key="ai_new_symbol")
    if c2.button("Add to Watchlist"):
        symbol_clean = new_symbol.strip()
        if symbol_clean and symbol_clean not in st.session_state.ai_watchlist:
            st.session_state.ai_watchlist.append(symbol_clean)

    for symbol in list(st.session_state.ai_watchlist):
        with st.expander(symbol, expanded=False):
            remove_col, _ = st.columns([1, 5])
            if remove_col.button("Remove", key=f"remove_{symbol}"):
                st.session_state.ai_watchlist.remove(symbol)
                st.rerun()

            df = fetch_history_cached(fyers, symbol, "15", 30)
            if df is None or df.empty:
                st.warning("Historical data unavailable.")
                continue

            try:
                e20_series = ema(df["close"], 20)
                e50_series = ema(df["close"], 50)
                e20 = e20_series.iloc[-1]
                e50 = e50_series.iloc[-1]
                e200 = ema(df["close"], 200).iloc[-1] if len(df) >= 200 else np.nan
                vwap_val = vwap(df).iloc[-1]
                rsi_val = rsi(df["close"]).iloc[-1]
                _, _, hist = macd(df["close"])
                adx_val = adx(df).iloc[-1]
                atr_val = atr(df).iloc[-1]
                support, resistance = support_resistance(df)
                close = df["close"].iloc[-1]

                avg_vol = df["volume"].tail(20).mean()
                rel_vol = round(df["volume"].iloc[-1] / avg_vol, 2) if avg_vol else 0.0
                vol_spike = rel_vol > 2.0

                golden_cross = len(df) >= 2 and e20 > e50 and e20_series.iloc[-2] <= e50_series.iloc[-2]
                death_cross = len(df) >= 2 and e20 < e50 and e20_series.iloc[-2] >= e50_series.iloc[-2]

                breakout = close > resistance
                breakdown = close < support

                institutional_buying = rel_vol > 1.5 and close > df["open"].iloc[-1]
                institutional_selling = rel_vol > 1.5 and close < df["open"].iloc[-1]

                c1a, c2a, c3a, c4a = st.columns(4)
                c1a.metric("EMA20", f"{e20:.2f}")
                c2a.metric("EMA50", f"{e50:.2f}")
                c3a.metric("EMA200", f"{e200:.2f}" if not np.isnan(e200) else "NA")
                c4a.metric("VWAP", f"{vwap_val:.2f}")

                c5a, c6a, c7a, c8a = st.columns(4)
                c5a.metric("RSI", f"{rsi_val:.1f}")
                c6a.metric("MACD Hist", f"{hist.iloc[-1]:.3f}")
                c7a.metric("ADX", f"{adx_val:.1f}")
                c8a.metric("ATR", f"{atr_val:.2f}")

                c9a, c10a = st.columns(2)
                c9a.metric("Support", f"{support:.2f}")
                c10a.metric("Resistance", f"{resistance:.2f}")

                tags = []
                if golden_cross:
                    tags.append("🟢 Golden Cross")
                if death_cross:
                    tags.append("🔴 Death Cross")
                if breakout:
                    tags.append("🚀 Breakout")
                if breakdown:
                    tags.append("⚠️ Breakdown")
                if vol_spike:
                    tags.append("📊 Volume Spike")
                if institutional_buying:
                    tags.append("🏦 Institutional Buying")
                if institutional_selling:
                    tags.append("🏦 Institutional Selling")

                st.write(f"Relative Volume: **{rel_vol}x**")
                st.write(" | ".join(tags) if tags else "No special patterns detected.")
            except Exception as exc:
                st.warning(f"Unable to compute indicators for {symbol} ({exc}).")


def render_smc_section(fyers: Any) -> None:
    """Section 10: Smart Money Concepts."""
    st.subheader("🧩 Smart Money Concepts (SMC)")
    watchlist = st.session_state.get("ai_watchlist", WATCHLIST_DEFAULT)
    options = [v for k, v in INDEX_SYMBOLS.items() if k != "INDIA VIX"] + list(watchlist)
    selected = st.selectbox("Select instrument for SMC analysis", options, key="smc_symbol_select")

    df = fetch_history_cached(fyers, selected, "15", 30)
    if df is None or df.empty:
        st.warning("Historical data unavailable for SMC analysis.")
        return

    smc = compute_smc(df)
    c1, c2 = st.columns(2)
    c1.metric("Swing High", f"{smc['swing_high']:.2f}" if smc["swing_high"] else "NA")
    c2.metric("Swing Low", f"{smc['swing_low']:.2f}" if smc["swing_low"] else "NA")

    c3, c4 = st.columns(2)
    c3.metric("BOS", smc["bos"])
    c4.metric("CHOCH", smc["choch"])

    st.write(f"**Order Block:** {smc['order_block']}")
    st.write(f"**Breaker Block:** {smc['breaker_block']}")
    st.write(f"**Mitigation Block:** {smc['mitigation_block']}")
    st.write(f"**Fair Value Gap:** {smc['fvg']}")
    st.write(f"**Liquidity Sweep:** {smc['liquidity_sweep']}")
    st.write(f"**Liquidity Pool:** {smc['liquidity_pool']}")
    st.write(f"**Equal High:** {'Yes' if smc['equal_high'] else 'No'}")
    st.write(f"**Equal Low:** {'Yes' if smc['equal_low'] else 'No'}")
    st.write(f"**CISD:** {smc['cisd']}")
    zone_emoji = "🔴" if smc["zone"] == "PREMIUM" else ("🟢" if smc["zone"] == "DISCOUNT" else "⚪")
    st.write(f"**Premium/Discount Zone:** {zone_emoji} {smc['zone']}")


def render_ai_prediction_section(fyers: Any, news_df: pd.DataFrame, fii_dii_net: float) -> None:
    """Section 11: AI Prediction."""
    st.subheader("🎯 AI Prediction")
    watchlist = st.session_state.get("ai_watchlist", WATCHLIST_DEFAULT)
    options = [v for k, v in INDEX_SYMBOLS.items() if k != "INDIA VIX"] + list(watchlist)
    selected = st.selectbox("Select instrument for AI prediction", options, key="prediction_symbol_select")

    df = fetch_history_cached(fyers, selected, "15", 30)
    if df is None or df.empty:
        st.warning("Historical data unavailable for prediction.")
        return

    try:
        close = float(df["close"].iloc[-1])
        e50 = ema(df["close"], 50).iloc[-1]
        trend_score = 60.0 if close > e50 else -60.0
        rsi_val = rsi(df["close"]).iloc[-1]
        _, _, hist = macd(df["close"])
        vwap_val = vwap(df).iloc[-1]
        adx_val = adx(df).iloc[-1]
        atr_val = atr(df).iloc[-1]

        smc = compute_smc(df)
        smc_score = 0.0
        if "BULLISH" in smc["bos"] or "BULLISH" in smc["choch"]:
            smc_score += 50
        if "BEARISH" in smc["bos"] or "BEARISH" in smc["choch"]:
            smc_score -= 50
        if smc["zone"] == "DISCOUNT":
            smc_score += 20
        elif smc["zone"] == "PREMIUM":
            smc_score -= 20

        if not news_df.empty:
            bull_n = (news_df["Sentiment"] == "Bullish").sum()
            bear_n = (news_df["Sentiment"] == "Bearish").sum()
            news_score = ((bull_n - bear_n) / max(1, len(news_df))) * 100
        else:
            news_score = 0.0

        fii_dii_score = max(-100.0, min(100.0, fii_dii_net / 50))

        avg_vol = df["volume"].tail(20).mean()
        rel_vol = df["volume"].iloc[-1] / avg_vol if avg_vol else 1.0
        volume_score = max(-100.0, min(100.0, (rel_vol - 1) * 50))
        institutional_score = volume_score * 0.5 if close > df["open"].iloc[-1] else -volume_score * 0.5

        prediction = generate_ai_prediction(
            ltp=close, trend_score=trend_score, news_score=news_score,
            fii_dii_score=fii_dii_score, rsi_val=rsi_val, macd_hist=hist.iloc[-1],
            vwap_val=vwap_val, adx_val=adx_val, smc_score=smc_score,
            volume_score=volume_score, institutional_score=institutional_score, atr_val=atr_val,
        )

        rec_colors = {"STRONG BUY": "🟢", "BUY": "🟢", "WATCH": "🟡", "SELL": "🔴", "STRONG SELL": "🔴"}
        st.markdown(f"### {rec_colors.get(prediction.recommendation, '⚪')} {prediction.recommendation}")

        c1, c2, c3 = st.columns(3)
        c1.metric("Confidence", f"{prediction.confidence}%")
        c2.metric("AI Score", f"{prediction.score}")
        c3.metric("Risk:Reward", f"1:{prediction.risk_reward}")

        c4, c5, c6 = st.columns(3)
        c4.metric("Target 1", f"{prediction.target1}")
        c5.metric("Target 2", f"{prediction.target2}")
        c6.metric("Stop Loss", f"{prediction.stoploss}")

        st.progress(min(1.0, max(0.0, prediction.confidence / 100)))
    except Exception as exc:
        st.warning(f"Unable to generate AI prediction for {selected} ({exc}).")


# ==============================================================================
# 11. MAIN ENTRY POINT
# ==============================================================================

def show_ai_market_intelligence(fyers: Any) -> None:
    """
    Public entry point. Renders the complete AI Market Intelligence dashboard.

    Call this from market.py:

        from ai_market_intelligence import show_ai_market_intelligence
        show_ai_market_intelligence(fyers)

    This function is fully self-contained, never raises, and does not modify
    scanner.py, option_chain.py, app.py, trading.py, or any existing
    BUY/SELL signal logic.
    """
    st.markdown("## 🧠 AI Market Intelligence")
    st.caption(f"Last updated: {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} · Auto-refresh: 60s")

    if _HAS_AUTOREFRESH:
        st_autorefresh(interval=REFRESH_INTERVAL_MS, key="ai_market_intel_autorefresh")
    else:
        st.caption("Install `streamlit-autorefresh` to enable automatic 60-second refresh.")

    symbols_tuple = tuple(INDEX_SYMBOLS.items())
    market_df = _safe_call(get_market_summary_cached, fyers, symbols_tuple, default=pd.DataFrame())

    vix_value: Optional[float] = None
    if not market_df.empty:
        vix_row = market_df[market_df["Index"] == "INDIA VIX"]
        if not vix_row.empty and pd.notna(vix_row["LTP"].iloc[0]):
            vix_value = float(vix_row["LTP"].iloc[0])

    news_df = _safe_call(fetch_all_news, default=pd.DataFrame())

    fii_dii_data = _safe_call(fetch_fii_dii, default=None)
    fii_dii_net = 0.0
    if fii_dii_data:
        fii_dii_net = (fii_dii_data["fii_buy"] - fii_dii_data["fii_sell"]) - (fii_dii_data["dii_buy"] - fii_dii_data["dii_sell"])

    score = compute_overall_market_score(market_df, vix_value, news_df)

    tabs = st.tabs([
        "🧠 AI Dashboard", "📊 Market Summary", "📈 India VIX", "📰 Live News",
        "🤖 News Analysis", "🏦 FII/DII", "📐 Index Analysis", "📉 Futures",
        "📌 Stocks", "🧩 SMC", "🎯 AI Prediction",
    ])

    with tabs[0]:
        _render_safely("AI Dashboard", render_ai_dashboard, score)
    with tabs[1]:
        market_only_df = market_df[market_df["Index"] != "INDIA VIX"] if not market_df.empty else market_df
        _render_safely("Market Summary", render_market_summary, market_only_df)
    with tabs[2]:
        _render_safely("India VIX Analysis", render_vix_analysis, vix_value)
    with tabs[3]:
        _render_safely("Live News", render_live_news, news_df)
    with tabs[4]:
        _render_safely("AI News Analysis", render_ai_news_analysis, news_df)
    with tabs[5]:
        _render_safely("FII DII", render_fii_dii)
    with tabs[6]:
        _render_safely("Index Analysis", render_index_analysis, fyers)
    with tabs[7]:
        _render_safely("Futures Analysis", render_futures_analysis, fyers)
    with tabs[8]:
        _render_safely("Stock Analysis", render_stock_analysis, fyers)
    with tabs[9]:
        _render_safely("Smart Money Concepts", render_smc_section, fyers)
    with tabs[10]:
        _render_safely("AI Prediction", render_ai_prediction_section, fyers, news_df, fii_dii_net)
