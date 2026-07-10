"""
ai_market_intelligence.py
==============================================================================
Standalone AI-powered "Market Intelligence" dashboard module for a FYERS
Streamlit trading dashboard.

DESIGN CONTRACT
--------------------------------------------------------------------------
This module is fully self-contained. It does NOT import, modify, or depend
on scanner.py, option_chain.py, app.py, market.py, or trading.py, and it
never touches any existing BUY/SELL signal logic. It exposes exactly one
public function:

    show_ai_market_intelligence(fyers)

which can be safely called from market.py, e.g.:

    from ai_market_intelligence import show_ai_market_intelligence
    show_ai_market_intelligence(fyers)

Every external call (FYERS API, RSS feeds, NSE endpoints) is wrapped in
try/except and retried, so a single bad response never crashes the
dashboard - failing sections show "Data Not Available" while the rest of
the app keeps working.

Reliability notes (v2):
- Every FYERS response is validated (`_is_valid_response`) before use.
- Failed API calls are retried with backoff (`_retry_call`).
- Live quotes and live option-chain data are NEVER cached (re-fetched on
  every rerun / auto-refresh tick). Only historical OHLCV candles and the
  RSS news feed are cached.
- The dashboard auto-refreshes every 30 seconds.
- Missing/invalid values render as "Data Not Available" instead of blanks.
- AI Market Direction is now computed from a broad, weighted multi-factor
  model (trend, option chain, SMC, news, institutional flow, momentum,
  volume) instead of any single indicator. BUY/SELL are only ever shown
  when the weighted Bullish/Bearish score exceeds 80% AND multiple
  independent factors confirm the same direction - otherwise the engine
  reports WAIT.

Reliability notes (v3 - Greeks / Global Markets / True Breadth):
- Added a real Options Greeks engine (Delta, Theta, Vega, IV, IV Rank,
  IV Percentile, Delta Exposure) sourced from FYERS option-chain greeks
  where available. IV history is cached locally (session-scoped) purely
  to derive IV Rank/Percentile; no external IV data source is invented.
- Added a Global Markets panel (Gift Nifty, USDINR, Crude, Gold, US
  Futures, Asian Markets, European Markets). FYERS symbols are used
  first; anything FYERS cannot serve (Gift Nifty / US / Asian / European
  indices) falls back to a best-effort public quote lookup, and shows
  "Data Not Available" rather than a guess if that also fails.
- Added a True Market Breadth engine (Advances/Declines across NIFTY 50
  constituents, Top Gainers/Losers, Volume Leaders, Sector Rotation via
  NSE sector indices, Heavyweight Contribution) alongside the original
  6-index proxy breadth count (kept as "Legacy Breadth" on the AI
  Dashboard tab for backward compatibility).
- These new factors (Greeks, Global Markets, True Breadth) are wired into
  the multi-factor AI Direction engine as confidence modifiers per the
  "professional rules" in the spec (e.g. Option Chain vs Greeks
  disagreement, Futures vs Spot disagreement, Global risk-off
  environment) WITHOUT changing the original 7-factor weighting, so the
  existing BUY/SELL/WAIT behavior for the original factors is preserved.

Dependencies: streamlit, pandas, numpy, requests, feedparser,
streamlit-autorefresh (optional - degrades gracefully if not installed).

Python: 3.11
==============================================================================
"""

from __future__ import annotations

import datetime as dt
import time
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

REFRESH_INTERVAL_MS: int = 30_000  # Auto Refresh every 30 seconds

DATA_NA: str = "Data Not Available"
LOADING_MSG: str = "Loading..."
FETCHING_MSG: str = "Fetching..."

DEFAULT_RETRIES: int = 2
RETRY_DELAY_SECONDS: float = 0.35

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

# Weights for the multi-factor AI Market Direction engine. Must sum to 1.0.
DIRECTION_WEIGHTS: Dict[str, float] = {
    "trend": 0.25,
    "option_chain": 0.20,
    "smc": 0.15,
    "news": 0.10,
    "institutional": 0.10,
    "momentum": 0.10,
    "volume": 0.10,
}

BUY_SELL_SCORE_THRESHOLD: float = 80.0
MIN_CONFIRMATIONS_REQUIRED: int = 4  # out of 6 directional factors (excludes volume)

# ------------------------------------------------------------------------
# NEW (v3): NIFTY 50 constituents (approximate free-float weights) used for
# True Market Breadth, Top Gainers/Losers, Volume Leaders, and Heavyweight
# Contribution. Weights are indicative (rounded, publicly-known approximate
# NIFTY50 index weights) - used only to rank "heavyweight contribution",
# never claimed as an official index-provider weight.
# ------------------------------------------------------------------------
NIFTY50_CONSTITUENTS: Dict[str, float] = {
    "NSE:RELIANCE-EQ": 9.5, "NSE:HDFCBANK-EQ": 12.5, "NSE:ICICIBANK-EQ": 8.5,
    "NSE:INFY-EQ": 5.5, "NSE:TCS-EQ": 4.0, "NSE:BHARTIARTL-EQ": 4.5,
    "NSE:ITC-EQ": 3.5, "NSE:LT-EQ": 3.5, "NSE:KOTAKBANK-EQ": 3.0,
    "NSE:AXISBANK-EQ": 3.0, "NSE:SBIN-EQ": 2.8, "NSE:BAJFINANCE-EQ": 2.5,
    "NSE:HINDUNILVR-EQ": 2.2, "NSE:MARUTI-EQ": 1.8, "NSE:SUNPHARMA-EQ": 1.7,
    "NSE:HCLTECH-EQ": 1.6, "NSE:TITAN-EQ": 1.4, "NSE:ULTRACEMCO-EQ": 1.3,
    "NSE:ASIANPAINT-EQ": 1.2, "NSE:NTPC-EQ": 1.2, "NSE:M&M-EQ": 1.5,
    "NSE:POWERGRID-EQ": 1.1, "NSE:WIPRO-EQ": 1.0, "NSE:ADANIENT-EQ": 1.0,
    "NSE:TATAMOTORS-EQ": 1.3, "NSE:TATASTEEL-EQ": 1.0, "NSE:JSWSTEEL-EQ": 0.9,
    "NSE:BAJAJFINSV-EQ": 0.9, "NSE:NESTLEIND-EQ": 0.9, "NSE:ONGC-EQ": 0.8,
    "NSE:COALINDIA-EQ": 0.8, "NSE:INDUSINDBK-EQ": 0.8, "NSE:GRASIM-EQ": 0.7,
    "NSE:TECHM-EQ": 0.7, "NSE:HINDALCO-EQ": 0.7, "NSE:DRREDDY-EQ": 0.6,
    "NSE:CIPLA-EQ": 0.6, "NSE:APOLLOHOSP-EQ": 0.6, "NSE:DIVISLAB-EQ": 0.6,
    "NSE:BRITANNIA-EQ": 0.5, "NSE:EICHERMOT-EQ": 0.5, "NSE:HEROMOTOCO-EQ": 0.5,
    "NSE:BPCL-EQ": 0.5, "NSE:SBILIFE-EQ": 0.6, "NSE:HDFCLIFE-EQ": 0.6,
    "NSE:UPL-EQ": 0.4, "NSE:BAJAJ-AUTO-EQ": 0.6, "NSE:SHRIRAMFIN-EQ": 0.5,
    "NSE:LTIM-EQ": 0.6, "NSE:ADANIPORTS-EQ": 0.7,
}

# NSE sector indices used for Sector Rotation (best-effort FYERS symbols;
# falls back to Data Not Available per-sector if a symbol is not servable).
SECTOR_INDICES: Dict[str, str] = {
    "NIFTY BANK": "NSE:NIFTYBANK-INDEX",
    "NIFTY IT": "NSE:NIFTYIT-INDEX",
    "NIFTY AUTO": "NSE:NIFTYAUTO-INDEX",
    "NIFTY PHARMA": "NSE:NIFTYPHARMA-INDEX",
    "NIFTY FMCG": "NSE:NIFTYFMCG-INDEX",
    "NIFTY METAL": "NSE:NIFTYMETAL-INDEX",
    "NIFTY ENERGY": "NSE:NIFTYENERGY-INDEX",
    "NIFTY REALTY": "NSE:NIFTYREALTY-INDEX",
    "NIFTY PSU BANK": "NSE:NIFTYPSUBANK-INDEX",
    "NIFTY FIN SERVICE": "NSE:FINNIFTY-INDEX",
}

# Global market symbols. FYERS-servable ones use FYERS symbols; anything
# FYERS does not provide (US/Asian/European indices, Gift Nifty) is looked
# up via a best-effort public quote endpoint and degrades to Data Not
# Available if that also fails - it is never invented.
GLOBAL_FYERS_SYMBOLS: Dict[str, str] = {
    "USDINR": "NSE:USDINR-INDEX",
    "Crude Oil (MCX)": "MCX:CRUDEOIL25AUGFUT",
    "Gold (MCX)": "MCX:GOLD25AUGFUT",
}

# Yahoo Finance tickers for indices FYERS cannot serve. Best-effort only.
GLOBAL_EXTERNAL_SYMBOLS: Dict[str, str] = {
    "Gift Nifty / SGX Nifty": "NIFTY_F1.NS",
    "Dow Futures": "YM=F",
    "S&P 500 Futures": "ES=F",
    "Nasdaq Futures": "NQ=F",
    "Nikkei 225": "^N225",
    "Hang Seng": "^HSI",
    "FTSE 100": "^FTSE",
    "DAX": "^GDAXI",
}

IV_HISTORY_LOOKBACK_DAYS: int = 252  # for IV Rank / IV Percentile


# ==============================================================================
# 2. GENERIC HELPERS / SAFE EXECUTION / RETRY / VALIDATION
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
        st.info(DATA_NA)


def _retry_call(fn, *args: Any, retries: int = DEFAULT_RETRIES, delay: float = RETRY_DELAY_SECONDS,
                 default: Any = None, **kwargs: Any) -> Any:
    """Call fn(*args, **kwargs), retrying on exception or falsy/None result."""
    last_result = default
    for attempt in range(retries + 1):
        try:
            result = fn(*args, **kwargs)
            if result is not None:
                return result
            last_result = result
        except Exception:
            last_result = default
        if attempt < retries:
            time.sleep(delay)
    return last_result


def _is_valid_response(resp: Any) -> bool:
    """Validate a FYERS API response before it is ever used."""
    try:
        return isinstance(resp, dict) and resp.get("s") == "ok"
    except Exception:
        return False


def _fmt_num(value: Any, decimals: int = 2, suffix: str = "") -> str:
    """Format a numeric value, or return DATA_NA if missing/invalid."""
    try:
        if value is None:
            return DATA_NA
        if isinstance(value, float) and (np.isnan(value) or np.isinf(value)):
            return DATA_NA
        return f"{float(value):.{decimals}f}{suffix}"
    except Exception:
        return DATA_NA


def _fmt_int(value: Any, suffix: str = "") -> str:
    try:
        if value is None:
            return DATA_NA
        if isinstance(value, float) and (np.isnan(value) or np.isinf(value)):
            return DATA_NA
        return f"{int(value):,}{suffix}"
    except Exception:
        return DATA_NA


def _fmt_text(value: Any) -> str:
    if value is None or (isinstance(value, str) and not value.strip()):
        return DATA_NA
    return str(value)


def _sentiment_color(label: str) -> str:
    return {"Bullish": "🟢", "Bearish": "🔴", "Neutral": "🟡"}.get(label, "⚪")


def _trend_color(trend: str) -> str:
    return {"UP": "🟢", "DOWN": "🔴", "FLAT": "🟡", "NA": "⚪"}.get(trend, "⚪")


# ==============================================================================
# 3. TECHNICAL INDICATORS (independent implementation - EMA, RSI, MACD, ADX,
#    ATR, VWAP, Support/Resistance, Supertrend)
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


def supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0) -> Tuple[pd.Series, pd.Series]:
    """
    Supertrend indicator.
    Returns (supertrend_line, direction) where direction is +1 (uptrend) or
    -1 (downtrend) for each candle.
    """
    try:
        atr_val = atr(df, period)
        hl2 = (df["high"] + df["low"]) / 2
        upperband = hl2 + multiplier * atr_val
        lowerband = hl2 - multiplier * atr_val

        final_upper = upperband.copy()
        final_lower = lowerband.copy()
        st_line = pd.Series(index=df.index, dtype=float)
        direction = pd.Series(index=df.index, dtype=int)
        close = df["close"]

        for i in range(len(df)):
            if i == 0:
                final_upper.iloc[i] = upperband.iloc[i]
                final_lower.iloc[i] = lowerband.iloc[i]
                direction.iloc[i] = 1
                st_line.iloc[i] = final_lower.iloc[i]
                continue

            final_upper.iloc[i] = (
                upperband.iloc[i]
                if (upperband.iloc[i] < final_upper.iloc[i - 1] or close.iloc[i - 1] > final_upper.iloc[i - 1])
                else final_upper.iloc[i - 1]
            )
            final_lower.iloc[i] = (
                lowerband.iloc[i]
                if (lowerband.iloc[i] > final_lower.iloc[i - 1] or close.iloc[i - 1] < final_lower.iloc[i - 1])
                else final_lower.iloc[i - 1]
            )

            if direction.iloc[i - 1] == 1 and close.iloc[i] < final_lower.iloc[i]:
                direction.iloc[i] = -1
            elif direction.iloc[i - 1] == -1 and close.iloc[i] > final_upper.iloc[i]:
                direction.iloc[i] = 1
            else:
                direction.iloc[i] = direction.iloc[i - 1]

            st_line.iloc[i] = final_lower.iloc[i] if direction.iloc[i] == 1 else final_upper.iloc[i]

        return st_line, direction
    except Exception:
        empty = pd.Series(dtype=float)
        return empty, empty


# ==============================================================================
# 4. FYERS DATA ACCESS LAYER (quotes, history, option chain)
#    Live data (quotes / option chain) is NEVER cached. Only historical
#    OHLCV candles are cached, since they don't change once a candle closes.
# ==============================================================================

def fetch_quote(_fyers: Any, symbol: str, retries: int = DEFAULT_RETRIES) -> Optional[Dict[str, Any]]:
    """Fetch a single LIVE quote from FYERS (never cached). Returns None on failure."""

    def _do() -> Optional[Dict[str, Any]]:
        resp = _fyers.quotes({"symbols": symbol})
        if _is_valid_response(resp) and resp.get("d"):
            v = resp["d"][0].get("v")
            if isinstance(v, dict):
                return v
        return None

    return _retry_call(_do, retries=retries, default=None)


def fetch_quotes_batch(_fyers: Any, symbols: List[str], retries: int = DEFAULT_RETRIES) -> Dict[str, Dict[str, Any]]:
    """
    Fetch multiple LIVE quotes in as few FYERS calls as possible (FYERS
    accepts a comma-separated symbol list in a single `quotes` call).
    Never cached. Returns a dict keyed by symbol; symbols that fail to
    resolve are simply omitted (caller should treat missing keys as
    Data Not Available).
    """
    out: Dict[str, Dict[str, Any]] = {}
    if not symbols:
        return out

    chunk_size = 50  # conservative batch size for FYERS quotes endpoint
    for i in range(0, len(symbols), chunk_size):
        chunk = symbols[i:i + chunk_size]

        def _do(chunk=chunk) -> Optional[Dict[str, Dict[str, Any]]]:
            resp = _fyers.quotes({"symbols": ",".join(chunk)})
            if _is_valid_response(resp) and resp.get("d"):
                result: Dict[str, Dict[str, Any]] = {}
                for item in resp["d"]:
                    sym = item.get("n") or item.get("symbol")
                    v = item.get("v")
                    if sym and isinstance(v, dict):
                        result[sym] = v
                return result if result else None
            return None

        chunk_result = _retry_call(_do, retries=retries, default=None)
        if chunk_result:
            out.update(chunk_result)
    return out


def fetch_history(_fyers: Any, symbol: str, resolution: str = "15", days: int = 30,
                   retries: int = DEFAULT_RETRIES) -> Optional[pd.DataFrame]:
    """Fetch OHLCV candles from FYERS history API (historical data). Returns None on failure."""

    def _do() -> Optional[pd.DataFrame]:
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
        if _is_valid_response(resp) and resp.get("candles"):
            df = pd.DataFrame(resp["candles"], columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s")
            if len(df) > 0:
                return df
        return None

    return _retry_call(_do, retries=retries, default=None)


@st.cache_data(ttl=120, show_spinner=False)
def fetch_history_cached(_fyers: Any, symbol: str, resolution: str, days: int) -> Optional[pd.DataFrame]:
    """Cached wrapper - historical candles only (safe to cache briefly)."""
    return fetch_history(_fyers, symbol, resolution, days)


def _compute_max_pain(option_chain: List[Dict[str, Any]]) -> Optional[float]:
    """Compute the Max Pain strike from a raw options-chain list."""
    try:
        strikes: Dict[float, Dict[str, float]] = {}
        for o in option_chain:
            strike = o.get("strike_price")
            if strike is None:
                continue
            oi = o.get("oi", 0) or 0
            opt_type = o.get("option_type")
            bucket = strikes.setdefault(float(strike), {"CE": 0.0, "PE": 0.0})
            if opt_type == "CE":
                bucket["CE"] += float(oi)
            elif opt_type == "PE":
                bucket["PE"] += float(oi)
        if not strikes:
            return None

        best_strike, min_pain = None, None
        for candidate in strikes:
            total_pain = 0.0
            for k, v in strikes.items():
                total_pain += v["CE"] * max(0.0, candidate - k)
                total_pain += v["PE"] * max(0.0, k - candidate)
            if min_pain is None or total_pain < min_pain:
                min_pain = total_pain
                best_strike = candidate
        return float(best_strike) if best_strike is not None else None
    except Exception:
        return None


def _infer_spot_price(chain: List[Dict[str, Any]], data: Dict[str, Any],
                       underlying_ltp: Optional[float] = None) -> Optional[float]:
    """
    Fallback spot-price calculator. Tries, in order:
      1. An explicit spot field on the payload (`data['spot']`, `data['ltp']`).
      2. A per-leg underlying field on the first chain row (`fp`, `ltp`, `underlyingValue`).
      3. Put-call parity: for the strike where a CE and PE both have a live
         price, spot ~= strike + (call_ltp - put_ltp). Uses the pair closest
         to being genuinely at-the-money (smallest |call_ltp - put_ltp|).
      4. `underlying_ltp`, if the caller already has a fresh quote for the
         underlying (e.g. from fetch_quote), as a last resort.
    Returns None (never a guess) if nothing above resolves.
    """
    try:
        if isinstance(data.get("spot"), (int, float)) and data.get("spot"):
            return float(data["spot"])
        if isinstance(data.get("ltp"), (int, float)) and data.get("ltp"):
            return float(data["ltp"])
    except Exception:
        pass

    try:
        if chain:
            first = chain[0]
            for key in ("fp", "ltp", "underlyingValue", "underlying_value"):
                val = first.get(key)
                if isinstance(val, (int, float)) and val:
                    return float(val)
    except Exception:
        pass

    try:
        by_strike: Dict[float, Dict[str, float]] = {}
        for o in chain:
            strike = o.get("strike_price")
            price = o.get("ltp")
            opt_type = o.get("option_type")
            if strike is None or price is None or opt_type not in ("CE", "PE"):
                continue
            bucket = by_strike.setdefault(float(strike), {})
            bucket[opt_type] = float(price)

        best_strike, best_gap = None, None
        for strike, prices in by_strike.items():
            if "CE" in prices and "PE" in prices:
                gap = abs(prices["CE"] - prices["PE"])
                if best_gap is None or gap < best_gap:
                    best_gap = gap
                    best_strike = strike
        if best_strike is not None:
            prices = by_strike[best_strike]
            return round(best_strike + (prices["CE"] - prices["PE"]), 2)
    except Exception:
        pass

    if underlying_ltp is not None:
        try:
            return float(underlying_ltp)
        except Exception:
            pass

    return None


def _infer_atm_strike(chain: List[Dict[str, Any]], spot: Optional[float]) -> Optional[float]:
    """Fallback ATM-strike calculator: closest strike to spot, or the
    middle of the available strike range if spot could not be resolved."""
    try:
        strikes_available = sorted({float(o["strike_price"]) for o in chain if o.get("strike_price") is not None})
        if not strikes_available:
            return None
        if spot:
            return min(strikes_available, key=lambda s: abs(s - spot))
        return strikes_available[len(strikes_available) // 2]
    except Exception:
        return None


def _infer_max_pain(chain: List[Dict[str, Any]]) -> Optional[float]:
    """Fallback Max Pain calculator. Delegates to _compute_max_pain (kept
    as a separate name for symmetry with the other _infer_* fallbacks)."""
    return _compute_max_pain(chain)


def _infer_pcr(chain: List[Dict[str, Any]], put_oi: Optional[float] = None,
               call_oi: Optional[float] = None) -> Optional[float]:
    """
    Fallback PCR calculator. Uses OI (put_oi/call_oi) if supplied/derivable;
    if OI is entirely absent from the payload (some FYERS responses omit it
    intraday for illiquid strikes), falls back to a volume-based PCR as a
    secondary liquidity-weighted proxy, clearly a different metric but
    better than "Data Not Available" when volume is present and OI is not.
    """
    try:
        if put_oi is None or call_oi is None:
            put_oi = sum(float(o.get("oi", 0) or 0) for o in chain if o.get("option_type") == "PE")
            call_oi = sum(float(o.get("oi", 0) or 0) for o in chain if o.get("option_type") == "CE")
        if call_oi:
            return round(put_oi / call_oi, 2)
    except Exception:
        pass

    try:
        put_vol = sum(float(o.get("volume", 0) or 0) for o in chain if o.get("option_type") == "PE")
        call_vol = sum(float(o.get("volume", 0) or 0) for o in chain if o.get("option_type") == "CE")
        if call_vol:
            return round(put_vol / call_vol, 2)
    except Exception:
        pass

    return None


_EMPTY_OPTION_CHAIN_METRICS: Dict[str, Any] = {
    "pcr": None, "total_oi": None, "ce_oi": None, "pe_oi": None,
    "put_writing": None, "call_writing": None, "oi_change_pct": None,
    "max_pain": None, "gamma_exposure": None, "delta_oi": None,
    # NEW (v3) Greeks Engine fields:
    "avg_call_delta": None, "avg_put_delta": None, "avg_theta": None,
    "avg_vega": None, "atm_iv": None, "iv_rank": None, "iv_percentile": None,
    "delta_exposure": None, "atm_strike": None, "chain_detail": None,
}


def fetch_option_chain_data(_fyers: Any, symbol: str, retries: int = DEFAULT_RETRIES,
                             underlying_ltp: Optional[float] = None) -> Dict[str, Any]:
    """
    Fetch LIVE option-chain analytics (never cached): PCR, total/CE/PE OI,
    OI change %, Max Pain, put/call writing activity, gamma exposure,
    delta-OI (PE OI - CE OI, a directional-flow proxy), and (v3) a full
    Greeks snapshot: average Call/Put Delta, Theta, Vega, ATM IV, IV Rank,
    IV Percentile, Delta Exposure, ATM strike, and a per-strike ATM/ITM/OTM
    classification table.

    `underlying_ltp` is OPTIONAL context (e.g. a spot quote the caller
    already fetched this tick). If omitted, spot/ATM/max-pain/PCR are all
    derived independently via the _infer_* fallback calculators below, so
    this function works standalone with just (fyers, symbol) exactly as
    before - the extra argument only sharpens the spot-price fallback
    chain when available, it never becomes a hard requirement.

    Always returns a fully-keyed dict; missing fields are None (never
    invented) so the UI can show "Data Not Available" where appropriate.
    """

    def _do() -> Optional[Dict[str, Any]]:
        resp = _fyers.optionchain({"symbol": symbol, "strikecount": 10, "timestamp": ""})
        if not _is_valid_response(resp):
            return None
        data = resp.get("data", {})
        chain = data.get("optionsChain", [])
        if not chain:
            return None

        spot = _infer_spot_price(chain, data, underlying_ltp=underlying_ltp)

        put_oi = sum(float(o.get("oi", 0) or 0) for o in chain if o.get("option_type") == "PE")
        call_oi = sum(float(o.get("oi", 0) or 0) for o in chain if o.get("option_type") == "CE")
        total_oi = put_oi + call_oi
        pcr = _infer_pcr(chain, put_oi=put_oi, call_oi=call_oi)
        put_writing = "Active" if put_oi > call_oi else "Weak"
        call_writing = "Active" if call_oi > put_oi else "Weak"

        put_oi_chg = sum(float(o.get("oich", o.get("oi_change", 0)) or 0) for o in chain if o.get("option_type") == "PE")
        call_oi_chg = sum(float(o.get("oich", o.get("oi_change", 0)) or 0) for o in chain if o.get("option_type") == "CE")
        oi_change_pct = None
        if total_oi:
            try:
                oi_change_pct = round(((put_oi_chg + call_oi_chg) / total_oi) * 100, 2)
            except Exception:
                oi_change_pct = None

        max_pain = _infer_max_pain(chain)

        # ---- Greeks Engine (v3) --------------------------------------
        atm_strike = _infer_atm_strike(chain, spot)

        gammas, deltas_ce, deltas_pe, thetas, vegas, ivs_atm = [], [], [], [], [], []
        chain_detail: List[Dict[str, Any]] = []
        for o in chain:
            greeks = o.get("greeks") if isinstance(o.get("greeks"), dict) else {}
            strike = o.get("strike_price")
            opt_type = o.get("option_type")
            delta_val = greeks.get("delta")
            theta_val = greeks.get("theta")
            vega_val = greeks.get("vega")
            gamma_val = greeks.get("gamma")
            iv_val = o.get("iv") if o.get("iv") is not None else greeks.get("iv")

            if gamma_val is not None:
                gammas.append(gamma_val)
            if theta_val is not None:
                thetas.append(theta_val)
            if vega_val is not None:
                vegas.append(vega_val)
            if opt_type == "CE" and delta_val is not None:
                deltas_ce.append(delta_val)
            if opt_type == "PE" and delta_val is not None:
                deltas_pe.append(delta_val)

            moneyness = "NONE"
            try:
                if strike is not None and spot:
                    strike_f = float(strike)
                    if atm_strike is not None and abs(strike_f - atm_strike) < 1e-6:
                        moneyness = "ATM"
                    elif opt_type == "CE":
                        moneyness = "ITM" if strike_f < spot else "OTM"
                    elif opt_type == "PE":
                        moneyness = "ITM" if strike_f > spot else "OTM"
            except Exception:
                moneyness = "NONE"

            if atm_strike is not None and strike is not None and abs(float(strike) - atm_strike) < 1e-6 and iv_val is not None:
                ivs_atm.append(iv_val)

            chain_detail.append({
                "Strike": strike, "Type": opt_type, "Moneyness": moneyness,
                "OI": o.get("oi"), "Volume": o.get("volume"),
                "Delta": delta_val, "Theta": theta_val, "Vega": vega_val,
                "Gamma": gamma_val, "IV": iv_val,
            })

        gamma_exposure = round(float(sum(gammas)), 4) if gammas else None
        avg_call_delta = round(float(np.mean(deltas_ce)), 4) if deltas_ce else None
        avg_put_delta = round(float(np.mean(deltas_pe)), 4) if deltas_pe else None
        avg_theta = round(float(np.mean(thetas)), 4) if thetas else None
        avg_vega = round(float(np.mean(vegas)), 4) if vegas else None
        atm_iv = round(float(np.mean(ivs_atm)), 2) if ivs_atm else None
        delta_oi = round(put_oi - call_oi, 0)

        delta_exposure = None
        try:
            if avg_call_delta is not None and avg_put_delta is not None:
                delta_exposure = round(call_oi * avg_call_delta + put_oi * avg_put_delta, 2)
        except Exception:
            delta_exposure = None

        iv_rank, iv_percentile = _compute_iv_rank_percentile(symbol, atm_iv)

        return {
            "pcr": pcr, "total_oi": int(total_oi) if total_oi else None,
            "ce_oi": int(call_oi) if call_oi else None, "pe_oi": int(put_oi) if put_oi else None,
            "put_writing": put_writing, "call_writing": call_writing,
            "oi_change_pct": oi_change_pct, "max_pain": max_pain,
            "gamma_exposure": gamma_exposure, "delta_oi": delta_oi,
            "avg_call_delta": avg_call_delta, "avg_put_delta": avg_put_delta,
            "avg_theta": avg_theta, "avg_vega": avg_vega, "atm_iv": atm_iv,
            "iv_rank": iv_rank, "iv_percentile": iv_percentile,
            "delta_exposure": delta_exposure, "atm_strike": atm_strike,
            "chain_detail": chain_detail,
        }

    result = _retry_call(_do, retries=retries, default=None)
    return result if result is not None else dict(_EMPTY_OPTION_CHAIN_METRICS)


def _compute_iv_rank_percentile(symbol: str, current_iv: Optional[float]) -> Tuple[Optional[float], Optional[float]]:
    """
    IV Rank / IV Percentile over the session's observed ATM-IV history for
    this symbol. This is intentionally SESSION-SCOPED (stored in
    st.session_state) rather than backed by an invented long-run IV
    history - there is no independent historical-IV data source wired into
    this module, so per the "never invent data" rule we only rank IV
    against what has actually been observed live in this session. Once
    enough observations accumulate this becomes a meaningful intraday IV
    Rank/Percentile; early in a session (few observations) it will
    naturally sit near 50 and should be read with that caveat.
    """
    try:
        if current_iv is None:
            return None, None
        key = f"_iv_hist_{symbol}"
        hist: List[float] = st.session_state.get(key, [])
        hist.append(float(current_iv))
        hist = hist[-IV_HISTORY_LOOKBACK_DAYS:]
        st.session_state[key] = hist

        if len(hist) < 3:
            return None, None

        hist_min, hist_max = min(hist), max(hist)
        iv_rank = round(((current_iv - hist_min) / (hist_max - hist_min)) * 100, 1) if hist_max > hist_min else 50.0
        iv_percentile = round((sum(1 for v in hist if v <= current_iv) / len(hist)) * 100, 1)
        return iv_rank, iv_percentile
    except Exception:
        return None, None


def get_market_summary(_fyers: Any, symbols: Dict[str, str]) -> pd.DataFrame:
    """Build the LIVE market-summary table (Section 2). Never cached."""
    rows: List[Dict[str, Any]] = []
    for name, sym in symbols.items():
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


def fetch_futures_oi_nse_fallback(underlying: str, retries: int = 1) -> Optional[Dict[str, Any]]:
    """
    NSE fallback for futures OI when FYERS does not return it on the quote.
    Per spec: "If Future OI is unavailable from FYERS, automatically use
    NSE Futures data. Never display Data Not Available without trying
    alternate sources." This hits NSE's public quote-derivative endpoint;
    if that also fails, the caller still falls back to Data Not Available
    (we never invent an OI figure).
    """

    def _do() -> Optional[Dict[str, Any]]:
        headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
        session = requests.Session()
        session.get("https://www.nseindia.com", headers=headers, timeout=5)
        resp = session.get(
            f"https://www.nseindia.com/api/quote-derivative?symbol={underlying}",
            headers=headers, timeout=5,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        stocks = data.get("stocks", [])
        if not stocks:
            return None
        # First entry is typically the near-month future.
        near = stocks[0].get("marketDeets", {}) or stocks[0].get("metadata", {})
        oi = near.get("openInterest") or stocks[0].get("metadata", {}).get("openInterest")
        oi_chg_pct = near.get("changeinOpenInterest") or 0.0
        if oi is None:
            return None
        return {"oi": float(oi), "oi_change_pct": float(oi_chg_pct)}

    return _retry_call(_do, retries=retries, default=None)


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
        "demand_zone": None, "supply_zone": None,
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
                result["demand_zone"] = f"{down_candles['low'].iloc[-1]:.2f} - {down_candles['high'].iloc[-1]:.2f}"
        elif close < last_swing_low:
            up_candles = df[df["close"] > df["open"]].tail(1)
            if not up_candles.empty:
                result["order_block"] = f"Bearish OB @ {up_candles['high'].iloc[-1]:.2f}"
                result["supply_zone"] = f"{up_candles['low'].iloc[-1]:.2f} - {up_candles['high'].iloc[-1]:.2f}"

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
    """Download and analyze the latest news headlines (Sections 4 & 5). Safe to cache briefly."""
    rows: List[Dict[str, Any]] = []
    for source, url in NEWS_FEEDS.items():
        try:
            feed = _retry_call(feedparser.parse, url, retries=1, default=None)
            if feed is None:
                continue
            for entry in feed.entries[:max_per_source]:
                title = getattr(entry, "title", "").strip()
                if not title:
                    continue
                published = getattr(entry, "published", "") or getattr(entry, "updated", "") or ""
                sentiment, confidence = _classify_sentiment(title)
                impact = _classify_impact(title)
                affected_index, affected_stocks = _affected_assets(title)
                category = "Global" if any(
                    kw in title.lower() for kw in ("fed", "federal reserve", "wall street", "china", "europe", "asia")
                ) else ("Economic" if any(
                    kw in title.lower() for kw in ("rbi", "gdp", "inflation", "budget", "policy")
                ) else "Corporate")
                rows.append({
                    "Time": published,
                    "Headline": title,
                    "Category": category,
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

    def _do() -> Optional[Dict[str, float]]:
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
        return None

    return _retry_call(_do, retries=1, default=None)


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
# 8b. GLOBAL MARKETS ENGINE (NEW v3)
# ==============================================================================
# FYERS-servable instruments (USDINR, MCX Crude/Gold) use the same
# never-cached quote path as everything else live. Instruments FYERS does
# not serve (Gift Nifty, US/Asian/European indices) fall back to a
# best-effort public quote lookup (Yahoo Finance's public quote endpoint,
# no key required) and show Data Not Available if that also fails - never
# invented.

@st.cache_data(ttl=20, show_spinner=False)
def _fetch_external_quote_cached(ticker: str) -> Optional[Dict[str, float]]:
    """Very short-lived cache (20s) for external global-market lookups, so a
    single slow/failed provider doesn't hammer the network on every rerun."""
    return _fetch_external_quote(ticker)


def _fetch_external_quote(ticker: str) -> Optional[Dict[str, float]]:
    def _do() -> Optional[Dict[str, float]]:
        headers = {"User-Agent": "Mozilla/5.0"}
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        resp = requests.get(url, headers=headers, timeout=5)
        if resp.status_code != 200:
            return None
        data = resp.json()
        result = data.get("chart", {}).get("result")
        if not result:
            return None
        meta = result[0].get("meta", {})
        price = meta.get("regularMarketPrice")
        prev_close = meta.get("previousClose") or meta.get("chartPreviousClose")
        if price is None:
            return None
        change = (price - prev_close) if prev_close else None
        change_pct = (change / prev_close * 100) if (change is not None and prev_close) else None
        return {"price": float(price), "change": change, "change_pct": change_pct}

    return _retry_call(_do, retries=1, default=None)


def get_global_markets_summary(_fyers: Any) -> pd.DataFrame:
    """Build the Global Markets table: USDINR, Crude, Gold (via FYERS) plus
    Gift Nifty, US Futures, Asian Markets, European Markets (best-effort
    external lookup). Never raises; missing instruments show Data Not
    Available rather than being invented."""
    rows: List[Dict[str, Any]] = []

    for name, sym in GLOBAL_FYERS_SYMBOLS.items():
        q = _safe_call(fetch_quote, _fyers, sym, default=None)
        if q:
            ltp = q.get("lp")
            change = q.get("ch")
            change_pct = q.get("chp")
            rows.append({"Instrument": name, "Source": "FYERS", "Price": ltp,
                         "Change": change, "Change %": change_pct})
        else:
            rows.append({"Instrument": name, "Source": "FYERS", "Price": None,
                         "Change": None, "Change %": None})

    for name, ticker in GLOBAL_EXTERNAL_SYMBOLS.items():
        q = _safe_call(_fetch_external_quote_cached, ticker, default=None)
        if q:
            rows.append({"Instrument": name, "Source": "External", "Price": q.get("price"),
                         "Change": q.get("change"), "Change %": q.get("change_pct")})
        else:
            rows.append({"Instrument": name, "Source": "External", "Price": None,
                         "Change": None, "Change %": None})

    return pd.DataFrame(rows)


def score_global_markets(global_df: Optional[pd.DataFrame]) -> float:
    """
    Global-market risk sentiment factor (-100..100). Averages the
    available Change % across US futures / Asian / European markets as a
    simple 'risk-on vs risk-off' proxy for the overnight/global backdrop.
    Returns 0.0 (neutral / no confirmation either way) if no global data
    could be resolved, rather than guessing.
    """
    try:
        if global_df is None or global_df.empty:
            return 0.0
        risk_rows = global_df[global_df["Instrument"].isin(
            ["Dow Futures", "S&P 500 Futures", "Nasdaq Futures", "Nikkei 225",
             "Hang Seng", "FTSE 100", "DAX", "Gift Nifty / SGX Nifty"]
        )]
        pct_values = risk_rows["Change %"].dropna()
        if pct_values.empty:
            return 0.0
        avg_pct = float(pct_values.mean())
        return max(-100.0, min(100.0, avg_pct * 20.0))
    except Exception:
        return 0.0


# ==============================================================================
# 8c. TRUE MARKET BREADTH ENGINE (NEW v3)
# ==============================================================================
# Replaces the old 6-index proxy count with genuine Advance/Decline,
# Top Gainers/Losers, Volume Leaders, Sector Rotation, and Heavyweight
# Contribution computed across the actual NIFTY 50 constituents.

def compute_true_market_breadth(_fyers: Any) -> Dict[str, Any]:
    """
    Fetch live quotes for the NIFTY 50 constituents in batch and derive:
    advances/declines/unchanged, top 5 gainers/losers, top 5 volume
    leaders, and each stock's approximate index-point contribution
    (weight x change %) ranked to show heavyweight contribution.
    Returns a fully-keyed dict; on total failure every field is empty /
    None so the UI shows Data Not Available instead of guessing.
    """
    empty: Dict[str, Any] = {
        "advances": None, "declines": None, "unchanged": None,
        "top_gainers": pd.DataFrame(), "top_losers": pd.DataFrame(),
        "volume_leaders": pd.DataFrame(), "heavyweight_contribution": pd.DataFrame(),
    }
    try:
        symbols = list(NIFTY50_CONSTITUENTS.keys())
        quotes = _safe_call(fetch_quotes_batch, _fyers, symbols, default={})
        if not quotes:
            return empty

        rows: List[Dict[str, Any]] = []
        for sym in symbols:
            q = quotes.get(sym)
            if not q:
                continue
            chp = q.get("chp")
            if chp is None:
                continue
            rows.append({
                "Symbol": sym.replace("NSE:", "").replace("-EQ", ""),
                "LTP": q.get("lp"), "Change %": chp, "Volume": q.get("volume", 0),
                "Weight": NIFTY50_CONSTITUENTS.get(sym, 0.0),
                "Contribution": round(chp * NIFTY50_CONSTITUENTS.get(sym, 0.0), 3),
            })
        if not rows:
            return empty

        df = pd.DataFrame(rows)
        advances = int((df["Change %"] > 0).sum())
        declines = int((df["Change %"] < 0).sum())
        unchanged = int((df["Change %"] == 0).sum())

        top_gainers = df.sort_values("Change %", ascending=False).head(5).reset_index(drop=True)
        top_losers = df.sort_values("Change %", ascending=True).head(5).reset_index(drop=True)
        volume_leaders = df.sort_values("Volume", ascending=False).head(5).reset_index(drop=True)
        heavyweight_contribution = df.reindex(
            df["Contribution"].abs().sort_values(ascending=False).index
        ).head(10).reset_index(drop=True)

        return {
            "advances": advances, "declines": declines, "unchanged": unchanged,
            "top_gainers": top_gainers, "top_losers": top_losers,
            "volume_leaders": volume_leaders, "heavyweight_contribution": heavyweight_contribution,
        }
    except Exception:
        return empty


def get_sector_rotation(_fyers: Any) -> pd.DataFrame:
    """Live Change % for each NSE sector index, ranked for Sector Rotation."""
    rows: List[Dict[str, Any]] = []
    for name, sym in SECTOR_INDICES.items():
        q = _safe_call(fetch_quote, _fyers, sym, default=None)
        rows.append({
            "Sector": name,
            "Change %": q.get("chp") if q else None,
            "LTP": q.get("lp") if q else None,
        })
    df = pd.DataFrame(rows)
    if not df.empty and df["Change %"].notna().any():
        df = df.sort_values("Change %", ascending=False, na_position="last").reset_index(drop=True)
    return df


def score_true_breadth(breadth: Dict[str, Any]) -> float:
    """True-breadth factor (-100..100) from Advances vs Declines."""
    try:
        adv, dec = breadth.get("advances"), breadth.get("declines")
        if adv is None or dec is None or (adv + dec) == 0:
            return 0.0
        return max(-100.0, min(100.0, ((adv - dec) / (adv + dec)) * 100.0))
    except Exception:
        return 0.0


# ==============================================================================
# 9. MULTI-FACTOR AI MARKET DIRECTION ENGINE
# ==============================================================================
# Replaces any single-indicator direction call. Every factor below is scored
# on a -100 (fully bearish) .. +100 (fully bullish) scale, then combined
# using the weights in DIRECTION_WEIGHTS. A separate Bullish Score and
# Bearish Score (each 0-100%) are then derived so that BUY/SELL can require
# a strict >80% conviction plus multi-factor confirmation, per policy.
#
# NOTE (v3): The original 7 factors and DIRECTION_WEIGHTS are UNCHANGED, so
# existing BUY/SELL/WAIT behavior for those factors is identical to before.
# Greeks, Global Markets, and True Breadth are new, independent factors
# that are surfaced on their own tabs and folded in ONLY as confidence
# modifiers via apply_professional_rules() below (per the spec's
# "professional rules": disagreement between data sources reduces
# confidence or forces WAIT) - they never silently change what would have
# been a BUY/SELL/WAIT verdict into the opposite verdict.
# ==============================================================================

def score_trend(close: float, e20: float, e50: float, e200: Optional[float],
                 vwap_val: float, adx_val: float, supertrend_dir: Optional[int]) -> float:
    """Trend factor: EMA stack, EMA200 bias, VWAP position, Supertrend, ADX strength."""
    try:
        score, count = 0.0, 0
        if not np.isnan(e20) and not np.isnan(e50):
            if close > e20 > e50:
                score += 100.0
            elif close < e20 < e50:
                score -= 100.0
            else:
                score += 30.0 if close > e50 else -30.0
            count += 1
        if e200 is not None and not np.isnan(e200):
            score += 50.0 if close > e200 else -50.0
            count += 1
        if not np.isnan(vwap_val):
            score += 40.0 if close > vwap_val else -40.0
            count += 1
        if supertrend_dir is not None and not (isinstance(supertrend_dir, float) and np.isnan(supertrend_dir)):
            score += 60.0 * float(supertrend_dir)
            count += 1
        avg = score / count if count else 0.0
        adx_safe = adx_val if adx_val and not np.isnan(adx_val) else 15.0
        strength_mult = min(1.3, max(0.6, adx_safe / 20.0))
        return max(-100.0, min(100.0, avg * strength_mult))
    except Exception:
        return 0.0


def score_option_chain(pcr: Optional[float], oi_change_pct: Optional[float],
                        spot: Optional[float], max_pain: Optional[float],
                        put_writing: Optional[str], call_writing: Optional[str]) -> float:
    """Option-chain factor: PCR, OI build-up, writer activity, Max Pain pull."""
    try:
        score, count = 0.0, 0
        if pcr is not None:
            if pcr > 1.2:
                score += 60.0
            elif pcr < 0.8:
                score -= 60.0
            else:
                score += (pcr - 1.0) * 100.0
            count += 1
        if put_writing == "Active" and call_writing == "Weak":
            score += 40.0
            count += 1
        elif call_writing == "Active" and put_writing == "Weak":
            score -= 40.0
            count += 1
        if oi_change_pct is not None:
            score += max(-40.0, min(40.0, oi_change_pct))
            count += 1
        if max_pain is not None and spot:
            diff_pct = (spot - max_pain) / max_pain * 100.0
            score += max(-30.0, min(30.0, -diff_pct * 3.0))
            count += 1
        return max(-100.0, min(100.0, score / count if count else 0.0))
    except Exception:
        return 0.0


def score_smc(smc: Dict[str, Any]) -> float:
    """Smart Money Concepts factor: BOS/CHOCH, premium/discount zone, liquidity sweep."""
    try:
        score = 0.0
        bos = smc.get("bos", "")
        choch = smc.get("choch", "")
        if "BULLISH" in bos:
            score += 60.0
        if "BEARISH" in bos:
            score -= 60.0
        if "BULLISH" in choch:
            score += 40.0
        if "BEARISH" in choch:
            score -= 40.0
        if smc.get("zone") == "DISCOUNT":
            score += 20.0
        elif smc.get("zone") == "PREMIUM":
            score -= 20.0
        sweep = smc.get("liquidity_sweep", "")
        if sweep.startswith("Sell-side"):
            score -= 15.0  # rejection from above a swing high -> bearish
        elif sweep.startswith("Buy-side"):
            score += 15.0  # rejection from below a swing low -> bullish
        return max(-100.0, min(100.0, score))
    except Exception:
        return 0.0


def score_news(news_df: Optional[pd.DataFrame]) -> float:
    """News-sentiment factor."""
    try:
        if news_df is None or news_df.empty:
            return 0.0
        bull = int((news_df["Sentiment"] == "Bullish").sum())
        bear = int((news_df["Sentiment"] == "Bearish").sum())
        total = max(1, len(news_df))
        return max(-100.0, min(100.0, ((bull - bear) / total) * 150.0))
    except Exception:
        return 0.0


def score_institutional(fii_dii_net: Optional[float], buildup: Optional[str],
                         rel_vol: Optional[float], bullish_candle: bool) -> float:
    """Institutional-activity factor: FII/DII net flow, futures build-up, volume-confirmed direction."""
    try:
        score, count = 0.0, 0
        if fii_dii_net is not None:
            score += max(-60.0, min(60.0, fii_dii_net / 40.0))
            count += 1
        if buildup:
            if buildup in ("Long Build Up", "Short Covering"):
                score += 50.0
                count += 1
            elif buildup in ("Short Build Up", "Long Unwinding"):
                score -= 50.0
                count += 1
        if rel_vol is not None and rel_vol > 1.5:
            score += 30.0 if bullish_candle else -30.0
            count += 1
        return max(-100.0, min(100.0, score / count if count else 0.0))
    except Exception:
        return 0.0


def score_momentum(rsi_val: float, macd_hist: float) -> float:
    """Momentum factor: RSI displacement from midline + MACD histogram."""
    try:
        rsi_score = (rsi_val - 50.0) / 50.0 * 100.0
        macd_score = max(-100.0, min(100.0, macd_hist * 1000.0))
        return max(-100.0, min(100.0, (rsi_score + macd_score) / 2.0))
    except Exception:
        return 0.0


def score_volume(rel_vol: Optional[float], bullish_candle: bool) -> float:
    """Volume factor: relative volume spike direction."""
    try:
        if rel_vol is None:
            return 0.0
        base = max(-100.0, min(100.0, (rel_vol - 1.0) * 60.0))
        return base if bullish_candle else -base
    except Exception:
        return 0.0


def score_greeks(option_chain: Dict[str, Any]) -> float:
    """
    NEW (v3) Greeks factor (-100..100, informational - not part of
    DIRECTION_WEIGHTS): call/put delta skew plus IV Rank extremes.
    A call-delta-heavy book (more aggregate delta on the call side than
    puts) reads bullish; sustained low IV Rank favors premium buying language
    (informational only, not itself directional) so it is weighted lightly.
    """
    try:
        call_delta = option_chain.get("avg_call_delta")
        put_delta = option_chain.get("avg_put_delta")
        iv_rank = option_chain.get("iv_rank")
        score, count = 0.0, 0
        if call_delta is not None and put_delta is not None:
            # call_delta in [0,1], put_delta in [-1,0] typically
            skew = call_delta + put_delta  # >0 => call-side heavier => bullish
            score += max(-100.0, min(100.0, skew * 150.0))
            count += 1
        if iv_rank is not None:
            # Extremely high IV rank slightly favors mean-reversion caution
            # (small, deliberately weak contribution - informational only).
            score += max(-15.0, min(15.0, (50.0 - iv_rank) * 0.3))
            count += 1
        return max(-100.0, min(100.0, score / count if count else 0.0))
    except Exception:
        return 0.0


def compute_weighted_direction(
    trend_score: float,
    option_chain_score: float,
    smc_score: float,
    news_score: float,
    institutional_score: float,
    momentum_score: float,
    volume_score: float,
    vix_value: Optional[float] = None,
    atr_pct: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Aggregate all seven weighted factors into a full AI Market Direction
    verdict: Bullish Score, Bearish Score, direction label (Strong
    Bullish/Bullish/Neutral/Bearish/Strong Bearish), confidence, probability
    up/down, market strength, risk %, opportunity %, and a strict
    BUY/SELL/WAIT recommendation.
    """
    try:
        raw = {
            "trend": trend_score, "option_chain": option_chain_score, "smc": smc_score,
            "news": news_score, "institutional": institutional_score,
            "momentum": momentum_score, "volume": volume_score,
        }
        components = {k: max(-100.0, min(100.0, float(v) if v is not None else 0.0)) for k, v in raw.items()}

        bullish_score = sum(DIRECTION_WEIGHTS[k] * max(0.0, components[k]) for k in DIRECTION_WEIGHTS)
        bearish_score = sum(DIRECTION_WEIGHTS[k] * max(0.0, -components[k]) for k in DIRECTION_WEIGHTS)
        net_score = bullish_score - bearish_score

        confirming_factors = ("trend", "smc", "option_chain", "news", "institutional", "momentum")
        confirmations = sum(
            1 for k in confirming_factors
            if abs(components[k]) >= 25.0 and (
                (net_score >= 0 and components[k] > 0) or (net_score < 0 and components[k] < 0)
            )
        )

        if net_score >= 50:
            direction = "Strong Bullish"
        elif net_score >= 20:
            direction = "Bullish"
        elif net_score <= -50:
            direction = "Strong Bearish"
        elif net_score <= -20:
            direction = "Bearish"
        else:
            direction = "Neutral"

        prob_up = round(max(5.0, min(95.0, 50 + net_score * 0.45)), 1)
        prob_down = round(100.0 - prob_up, 1)
        market_strength = round(min(100.0, bullish_score + bearish_score), 1)

        vix_for_calc = vix_value if vix_value else 15.0
        risk_pct = round(max(5.0, min(95.0, vix_for_calc * 3.0 + (atr_pct or 0.0) * 2.0)), 1)
        opportunity_pct = round(max(5.0, min(95.0, 50 + net_score / 2.0)), 1)
        confidence = round(max(30.0, min(97.0, 50 + max(bullish_score, bearish_score) * 0.4 + confirmations * 3.0)), 1)

        if bullish_score > BUY_SELL_SCORE_THRESHOLD and confirmations >= MIN_CONFIRMATIONS_REQUIRED:
            recommendation = "STRONG BUY" if bullish_score >= 90 else "BUY"
        elif bearish_score > BUY_SELL_SCORE_THRESHOLD and confirmations >= MIN_CONFIRMATIONS_REQUIRED:
            recommendation = "STRONG SELL" if bearish_score >= 90 else "SELL"
        else:
            recommendation = "WAIT"

        return {
            "components": components,
            "bullish_score": round(bullish_score, 1),
            "bearish_score": round(bearish_score, 1),
            "net_score": round(net_score, 1),
            "direction": direction,
            "prob_up": prob_up,
            "prob_down": prob_down,
            "market_strength": market_strength,
            "risk_pct": risk_pct,
            "opportunity_pct": opportunity_pct,
            "confidence": confidence,
            "recommendation": recommendation,
            "confirmations": confirmations,
            "confirmations_required": MIN_CONFIRMATIONS_REQUIRED,
            "professional_notes": [],
        }
    except Exception:
        return {
            "components": {k: 0.0 for k in DIRECTION_WEIGHTS},
            "bullish_score": 0.0, "bearish_score": 0.0, "net_score": 0.0,
            "direction": "Neutral", "prob_up": 50.0, "prob_down": 50.0,
            "market_strength": 0.0, "risk_pct": 50.0, "opportunity_pct": 50.0,
            "confidence": 50.0, "recommendation": "WAIT", "confirmations": 0,
            "confirmations_required": MIN_CONFIRMATIONS_REQUIRED,
            "professional_notes": [],
        }


def apply_professional_rules(
    direction: Dict[str, Any],
    greeks_score: float,
    global_score: float,
    breadth_score: float,
    futures_vs_spot_agree: Optional[bool],
    option_chain_vs_futures_agree: Optional[bool],
    greeks_vs_oi_agree: Optional[bool],
) -> Dict[str, Any]:
    """
    NEW (v3): Apply the spec's "Professional Rules" (Step 12) as
    post-hoc confidence modifiers on top of the existing 7-factor verdict,
    WITHOUT re-weighting or overturning it into the opposite direction:

      - If Futures and Spot disagree -> reduce confidence.
      - If Option Chain disagrees with Futures -> force WAIT.
      - If Greeks disagree with OI -> force WAIT.
      - A strongly risk-off Global Markets backdrop or negative True
        Breadth alongside a BUY (or the mirror image for SELL) also
        reduces confidence, since the spec calls for comparing Spot,
        Futures, Option Chain, Greeks, FII/DII, Breadth, and Global
        Markets before concluding.

    Any unknown (None) agreement flag is treated as "insufficient data to
    check" and is skipped rather than assumed to agree or disagree.
    """
    out = dict(direction)
    notes: List[str] = list(out.get("professional_notes", []))
    confidence = out.get("confidence", 50.0)
    recommendation = out.get("recommendation", "WAIT")

    if futures_vs_spot_agree is False:
        confidence = max(20.0, confidence - 15.0)
        notes.append("Futures and Spot are disagreeing on direction - confidence reduced.")

    if option_chain_vs_futures_agree is False:
        if recommendation not in ("WAIT",):
            notes.append("Option Chain and Futures are disagreeing on direction - overridden to WAIT.")
        recommendation = "WAIT"

    if greeks_vs_oi_agree is False:
        if recommendation not in ("WAIT",):
            notes.append("Greeks and Open Interest are disagreeing on direction - overridden to WAIT.")
        recommendation = "WAIT"

    net_score = out.get("net_score", 0.0)
    if net_score > 0 and (global_score < -25.0 or breadth_score < -25.0):
        confidence = max(20.0, confidence - 10.0)
        notes.append("Bullish verdict against a risk-off Global Markets / weak True Breadth backdrop - confidence reduced.")
    elif net_score < 0 and (global_score > 25.0 or breadth_score > 25.0):
        confidence = max(20.0, confidence - 10.0)
        notes.append("Bearish verdict against a risk-on Global Markets / strong True Breadth backdrop - confidence reduced.")

    out["confidence"] = round(confidence, 1)
    out["recommendation"] = recommendation
    out["professional_notes"] = notes
    out["greeks_score"] = round(greeks_score, 1)
    out["global_score"] = round(global_score, 1)
    out["breadth_score"] = round(breadth_score, 1)
    return out


@dataclass
class InstrumentSignals:
    """Bundle of everything needed to score one instrument (index/stock/future)."""
    close: float
    e20: float
    e50: float
    e200: Optional[float]
    vwap_val: float
    rsi_val: float
    macd_hist: float
    adx_val: float
    atr_val: float
    supertrend_dir: Optional[int]
    support: float
    resistance: float
    smc: Dict[str, Any]
    option_chain: Dict[str, Any]
    rel_vol: Optional[float]
    bullish_candle: bool
    buildup: Optional[str]


def evaluate_instrument_direction(
    signals: Any,
    news_df: Optional[pd.DataFrame] = None,
    fii_dii_net: Optional[float] = None,
    vix_value: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Run every factor scorer for one instrument and produce the final AI verdict.

    Backward/forward compatible signature:
      - `signals` may be an InstrumentSignals dataclass (original contract)
        OR a plain dict with the same field names - useful when calling
        this from a context that only has raw data on hand (e.g. a REPL,
        a test, or another module that doesn't want to import the
        dataclass). Unknown/missing dict keys fall back to safe neutral
        defaults rather than raising.
      - `news_df` / `fii_dii_net` / `vix_value` are now OPTIONAL. If
        omitted, they're fetched internally via the module's own
        (cached/safe) accessors so this function can be called with just
        `signals` and still produce a complete verdict - it no longer
        requires the caller to have already assembled all four arguments.
    """
    if isinstance(signals, dict):
        defaults = {
            "close": 0.0, "e20": 0.0, "e50": 0.0, "e200": None, "vwap_val": 0.0,
            "rsi_val": 50.0, "macd_hist": 0.0, "adx_val": 15.0, "atr_val": 0.0,
            "supertrend_dir": None, "support": 0.0, "resistance": 0.0,
            "smc": {}, "option_chain": dict(_EMPTY_OPTION_CHAIN_METRICS),
            "rel_vol": None, "bullish_candle": True, "buildup": None,
        }
        merged = {**defaults, **signals}
        signals = InstrumentSignals(**{k: merged[k] for k in defaults})

    if news_df is None:
        news_df = _safe_call(fetch_all_news, default=pd.DataFrame())

    if fii_dii_net is None:
        fii_dii_data = _safe_call(fetch_fii_dii, default=None)
        fii_dii_net = (
            (fii_dii_data["fii_buy"] - fii_dii_data["fii_sell"]) - (fii_dii_data["dii_buy"] - fii_dii_data["dii_sell"])
            if fii_dii_data else 0.0
        )

    trend_s = score_trend(signals.close, signals.e20, signals.e50, signals.e200,
                           signals.vwap_val, signals.adx_val, signals.supertrend_dir)
    oc_s = score_option_chain(
        signals.option_chain.get("pcr"), signals.option_chain.get("oi_change_pct"),
        signals.close, signals.option_chain.get("max_pain"),
        signals.option_chain.get("put_writing"), signals.option_chain.get("call_writing"),
    )
    smc_s = score_smc(signals.smc)
    news_s = score_news(news_df)
    inst_s = score_institutional(fii_dii_net, signals.buildup, signals.rel_vol, signals.bullish_candle)
    mom_s = score_momentum(signals.rsi_val, signals.macd_hist)
    vol_s = score_volume(signals.rel_vol, signals.bullish_candle)

    atr_pct = None
    try:
        if signals.close:
            atr_pct = (signals.atr_val / signals.close) * 100.0
    except Exception:
        atr_pct = None

    return compute_weighted_direction(
        trend_score=trend_s, option_chain_score=oc_s, smc_score=smc_s, news_score=news_s,
        institutional_score=inst_s, momentum_score=mom_s, volume_score=vol_s,
        vix_value=vix_value, atr_pct=atr_pct,
    )


def check_agreement(a_direction_positive: Optional[bool], b_direction_positive: Optional[bool]) -> Optional[bool]:
    """Helper: compare two directional booleans; None if either is unknown."""
    if a_direction_positive is None or b_direction_positive is None:
        return None
    return a_direction_positive == b_direction_positive


# ==============================================================================
# LEGACY BREADTH SCORE (kept for backward compatibility on the AI Dashboard
# tab - a simple count of the 6 tracked indices' Change% sign. Superseded
# by compute_true_market_breadth(), which scans the actual NIFTY 50
# constituents for genuine Advance/Decline breadth.)
# ==============================================================================

def compute_market_breadth(market_df: pd.DataFrame) -> Dict[str, int]:
    try:
        changes = market_df["Change %"].dropna() if not market_df.empty else pd.Series(dtype=float)
        bullish = int((changes > 0).sum())
        bearish = int((changes < 0).sum())
        neutral = int((changes == 0).sum()) + (int(market_df["Change %"].isna().sum()) if not market_df.empty else 0)
        return {"bullish": bullish, "bearish": bearish, "neutral": neutral}
    except Exception:
        return {"bullish": 0, "bearish": 0, "neutral": 0}


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
# 10. SHARED INSTRUMENT ANALYSIS HELPER
# ==============================================================================

def _analyze_instrument(fyers: Any, symbol: str) -> Optional[Dict[str, Any]]:
    """
    Fetch history + compute every indicator/SMC/option-chain field needed for
    both the Index/Stock views and the AI Direction engine. Returns None if
    historical data is unavailable (caller shows DATA_NA).
    """
    df = fetch_history_cached(fyers, symbol, "15", 30)
    if df is None or df.empty or len(df) < 5:
        return None

    e20_series = ema(df["close"], 20)
    e50_series = ema(df["close"], 50)
    e20 = float(e20_series.iloc[-1])
    e50 = float(e50_series.iloc[-1])
    e200 = float(ema(df["close"], 200).iloc[-1]) if len(df) >= 200 else None
    vwap_series = vwap(df)
    vwap_val = float(vwap_series.iloc[-1])
    rsi_val = float(rsi(df["close"]).iloc[-1])
    macd_line, macd_signal, macd_hist = macd(df["close"])
    adx_val = float(adx(df).iloc[-1])
    atr_val = float(atr(df).iloc[-1])
    support, resistance = support_resistance(df)
    st_line, st_dir = supertrend(df)
    supertrend_dir = int(st_dir.iloc[-1]) if len(st_dir) else None
    close = float(df["close"].iloc[-1])
    open_ = float(df["open"].iloc[-1])
    bullish_candle = close > open_

    avg_vol = df["volume"].tail(20).mean()
    rel_vol = round(float(df["volume"].iloc[-1] / avg_vol), 2) if avg_vol else None

    smc = compute_smc(df)
    option_chain = _safe_call(fetch_option_chain_data, fyers, symbol, default=dict(_EMPTY_OPTION_CHAIN_METRICS))

    return {
        "df": df, "e20": e20, "e50": e50, "e200": e200, "vwap": vwap_val,
        "rsi": rsi_val, "macd_line": macd_line, "macd_signal": macd_signal, "macd_hist": macd_hist,
        "adx": adx_val, "atr": atr_val, "support": support, "resistance": resistance,
        "supertrend_line": st_line, "supertrend_dir": supertrend_dir,
        "close": close, "open": open_, "bullish_candle": bullish_candle,
        "rel_vol": rel_vol, "smc": smc, "option_chain": option_chain,
        "breakout": close > resistance, "breakdown": close < support,
    }


# ==============================================================================
# 11. UI RENDER FUNCTIONS
# ==============================================================================

def render_ai_dashboard(direction: Dict[str, Any], breadth: Dict[str, int], true_breadth: Dict[str, Any]) -> None:
    """Section 1: AI Dashboard - overall AI Market Direction."""
    st.subheader("🧠 AI Dashboard")

    status_emoji = {
        "Strong Bullish": "🟢🟢", "Bullish": "🟢", "Neutral": "🟡",
        "Bearish": "🔴", "Strong Bearish": "🔴🔴",
    }.get(direction["direction"], "⚪")
    st.markdown(f"### Market Direction: {status_emoji} **{direction['direction']}**")

    rec_emoji = {"STRONG BUY": "🟢", "BUY": "🟢", "WAIT": "🟡", "SELL": "🔴", "STRONG SELL": "🔴"}.get(
        direction["recommendation"], "⚪"
    )
    st.markdown(
        f"**AI Signal:** {rec_emoji} {direction['recommendation']}  "
        f"·  Confirmations: {direction['confirmations']}/{direction['confirmations_required']} required"
    )

    for note in direction.get("professional_notes", []):
        st.caption(f"ℹ️ {note}")

    c1, c2, c3 = st.columns(3)
    c1.metric("Bullish Breadth (Legacy, 6 Indices)", breadth["bullish"])
    c2.metric("Bearish Breadth (Legacy, 6 Indices)", breadth["bearish"])
    c3.metric("Neutral Breadth (Legacy, 6 Indices)", breadth["neutral"])

    adv, dec, unch = true_breadth.get("advances"), true_breadth.get("declines"), true_breadth.get("unchanged")
    c1b, c2b, c3b = st.columns(3)
    c1b.metric("Advances (NIFTY 50)", adv if adv is not None else DATA_NA)
    c2b.metric("Declines (NIFTY 50)", dec if dec is not None else DATA_NA)
    c3b.metric("Unchanged (NIFTY 50)", unch if unch is not None else DATA_NA)

    c4, c5, c6 = st.columns(3)
    c4.metric("Bullish Score %", f"{direction['bullish_score']}%")
    c4.progress(min(1.0, max(0.0, direction["bullish_score"] / 100)))
    c5.metric("Bearish Score %", f"{direction['bearish_score']}%")
    c5.progress(min(1.0, max(0.0, direction["bearish_score"] / 100)))
    c6.metric("Confidence %", f"{direction['confidence']}%")
    c6.progress(min(1.0, max(0.0, direction["confidence"] / 100)))

    c7, c8, c9 = st.columns(3)
    c7.metric("Probability Up %", f"{direction['prob_up']}%")
    c8.metric("Probability Down %", f"{direction['prob_down']}%")
    c9.metric("Market Strength %", f"{direction['market_strength']}%")

    c10, c11 = st.columns(2)
    c10.metric("Risk %", f"{direction['risk_pct']}%")
    c10.progress(min(1.0, max(0.0, direction["risk_pct"] / 100)))
    c11.metric("Opportunity %", f"{direction['opportunity_pct']}%")
    c11.progress(min(1.0, max(0.0, direction["opportunity_pct"] / 100)))

    with st.expander("AI Direction Factor Breakdown (NIFTY-based composite)"):
        comp_df = pd.DataFrame(
            [{"Factor": k.replace("_", " ").title(), "Weight %": DIRECTION_WEIGHTS[k] * 100,
              "Score (-100..100)": direction["components"].get(k, 0.0)} for k in DIRECTION_WEIGHTS]
        )
        st.dataframe(comp_df, use_container_width=True, hide_index=True)
        st.caption(
            "BUY requires Bullish Score > 80% with at least "
            f"{direction['confirmations_required']} of 6 factors confirming. "
            "SELL requires the same on the Bearish Score. Otherwise the AI shows WAIT. "
            "Greeks / Global Markets / True Breadth (below) act only as confidence "
            "modifiers on this verdict, per the professional cross-check rules."
        )
        if "greeks_score" in direction:
            extra_df = pd.DataFrame([
                {"Factor": "Greeks (informational)", "Score (-100..100)": direction.get("greeks_score")},
                {"Factor": "Global Markets (informational)", "Score (-100..100)": direction.get("global_score")},
                {"Factor": "True Breadth (informational)", "Score (-100..100)": direction.get("breadth_score")},
            ])
            st.dataframe(extra_df, use_container_width=True, hide_index=True)


def render_market_summary(market_df: pd.DataFrame) -> None:
    """Section 2: Market Summary."""
    st.subheader("📊 Market Summary (Live)")
    if market_df.empty:
        st.warning(DATA_NA)
        return
    for _, row in market_df.iterrows():
        color = _trend_color(row.get("Trend", "NA"))
        with st.container():
            cols = st.columns([1.4, 1, 1, 1, 1, 1, 1, 1, 1])
            cols[0].markdown(f"**{color} {row['Index']}**")
            cols[1].metric("LTP", _fmt_num(row["LTP"]))
            cols[2].metric("Change", _fmt_num(row["Change"]))
            cols[3].metric("Change %", _fmt_num(row["Change %"], suffix="%"))
            cols[4].write(f"High: {_fmt_num(row['High'])}")
            cols[5].write(f"Low: {_fmt_num(row['Low'])}")
            cols[6].write(f"Open: {_fmt_num(row['Open'])}")
            cols[7].write(f"Prev: {_fmt_num(row['Prev Close'])}")
            cols[8].write(f"Vol: {_fmt_int(row['Volume'])}")
        st.divider()


def render_vix_analysis(vix_value: Optional[float]) -> None:
    """Section 3: India VIX Analysis."""
    st.subheader("📈 India VIX Analysis")
    if vix_value is None:
        st.warning(DATA_NA)
        return
    info = classify_vix(vix_value)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("India VIX", _fmt_num(vix_value))
    c2.metric("Regime", _fmt_text(info["regime"]))
    c3.metric("Risk %", f"{info['risk_pct']}%")
    c4.metric("Confidence %", f"{info['confidence_pct']}%")
    st.info(f"**Suggested Position Size:** {info['position_size']}")


def render_live_news(news_df: pd.DataFrame) -> None:
    """Section 4: Live News."""
    st.subheader("📰 Live News")
    if news_df.empty:
        st.warning(DATA_NA)
        return
    display_df = news_df[["Time", "Headline", "Category", "Source", "Sentiment", "Confidence"]].copy()
    display_df["Sentiment"] = display_df["Sentiment"].apply(lambda s: f"{_sentiment_color(s)} {s}")
    display_df["Time"] = display_df["Time"].apply(lambda t: t if t else DATA_NA)
    st.dataframe(display_df, use_container_width=True, hide_index=True)


def render_ai_news_analysis(news_df: pd.DataFrame) -> None:
    """Section 5: AI News Analysis."""
    st.subheader("🤖 AI News Analysis")
    if news_df.empty:
        st.warning(DATA_NA)
        return
    c1, c2, c3 = st.columns(3)
    c1.metric("Bullish", int((news_df["Sentiment"] == "Bullish").sum()))
    c2.metric("Bearish", int((news_df["Sentiment"] == "Bearish").sum()))
    c3.metric("Neutral", int((news_df["Sentiment"] == "Neutral").sum()))

    c4, c5, c6 = st.columns(3)
    c4.metric("High Impact", int((news_df["Impact"] == "High").sum()))
    c5.metric("Medium Impact", int((news_df["Impact"] == "Medium").sum()))
    c6.metric("Low Impact", int((news_df["Impact"] == "Low").sum()))

    avg_conf = news_df["Confidence"].mean() if "Confidence" in news_df.columns and not news_df.empty else None
    st.metric("Avg. Sentiment Confidence %", _fmt_num(avg_conf, decimals=1, suffix="%") if avg_conf is not None else DATA_NA)

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
            with st.spinner(f"{FETCHING_MSG} {name} data..."):
                a = _analyze_instrument(fyers, symbol)
            if a is None:
                st.warning(DATA_NA)
                continue
            try:
                trend = "UP" if a["close"] > a["e50"] else "DOWN"

                c1, c2, c3, c4 = st.columns(4)
                c1.metric("Trend", trend)
                c2.metric("EMA20", _fmt_num(a["e20"]))
                c3.metric("EMA50", _fmt_num(a["e50"]))
                c4.metric("EMA200", _fmt_num(a["e200"]) if a["e200"] is not None else DATA_NA)

                c5, c6, c7, c8 = st.columns(4)
                c5.metric("VWAP", _fmt_num(a["vwap"]))
                c6.metric("RSI", _fmt_num(a["rsi"], decimals=1))
                c7.metric("MACD Hist", _fmt_num(a["macd_hist"].iloc[-1], decimals=3))
                c8.metric("ADX", _fmt_num(a["adx"], decimals=1))

                c9, c10, c11, c12 = st.columns(4)
                c9.metric("ATR", _fmt_num(a["atr"]))
                c10.metric("Support", _fmt_num(a["support"]))
                c11.metric("Resistance", _fmt_num(a["resistance"]))
                st_dir_label = "UP" if a["supertrend_dir"] == 1 else ("DOWN" if a["supertrend_dir"] == -1 else DATA_NA)
                c12.metric("Supertrend", st_dir_label)

                oc = a["option_chain"]
                c13, c14, c15, c16 = st.columns(4)
                c13.metric("PCR", _fmt_num(oc.get("pcr")) if oc.get("pcr") is not None else DATA_NA)
                c14.metric("Total OI", _fmt_int(oc.get("total_oi")))
                c15.metric("Max Pain", _fmt_num(oc.get("max_pain")) if oc.get("max_pain") is not None else DATA_NA)
                c16.metric("Delta OI (PE-CE)", _fmt_int(oc.get("delta_oi")))

                c17, c18 = st.columns(2)
                c17.metric("Breakout", "Yes 🚀" if a["breakout"] else "No")
                c18.metric("Breakdown", "Yes ⚠️" if a["breakdown"] else "No")

                st.caption(
                    f"Put Writing: {_fmt_text(oc.get('put_writing'))} · "
                    f"Call Writing: {_fmt_text(oc.get('call_writing'))} · "
                    f"OI Change: {_fmt_num(oc.get('oi_change_pct'), suffix='%') if oc.get('oi_change_pct') is not None else DATA_NA}"
                )
            except Exception as exc:
                st.warning(f"Unable to complete technical analysis for {name} ({exc}). {DATA_NA}")


def render_futures_analysis(fyers: Any) -> None:
    """Section 8: Futures Analysis."""
    st.subheader("📉 Futures Analysis")
    for label, underlying in FUTURES_UNDERLYINGS.items():
        with st.expander(label, expanded=False):
            try:
                fut_symbol = _current_month_future_symbol(underlying)
                with st.spinner(f"{FETCHING_MSG} {label}..."):
                    q = fetch_quote(fyers, fut_symbol)
                if not q:
                    st.warning(f"{DATA_NA} for {label} ({fut_symbol}).")
                    continue
                price_change_pct = q.get("chp", 0.0)
                oi = q.get("oi")
                oi_change_pct = q.get("oipercent")
                oi_source = "FYERS"

                if oi is None or oi_change_pct is None:
                    # Per spec: never show Data Not Available without trying
                    # an alternate source first.
                    fallback = _safe_call(fetch_futures_oi_nse_fallback, underlying, default=None)
                    if fallback:
                        oi = fallback.get("oi") if oi is None else oi
                        oi_change_pct = fallback.get("oi_change_pct") if oi_change_pct is None else oi_change_pct
                        oi_source = "NSE (fallback)"

                volume = q.get("volume", 0)
                have_oi_data = oi is not None and oi_change_pct is not None
                buildup = classify_futures_buildup(price_change_pct, oi_change_pct) if have_oi_data else "Data Not Available"
                strength = min(100.0, abs(price_change_pct) * 10 + abs(oi_change_pct or 0.0) * 5) if have_oi_data else None

                if not have_oi_data:
                    signal = "WAIT"
                elif buildup in ("Long Build Up", "Short Covering"):
                    signal = "BUY"
                elif buildup in ("Short Build Up", "Long Unwinding"):
                    signal = "SELL"
                else:
                    signal = "WAIT"

                c1, c2, c3 = st.columns(3)
                c1.metric("Build Up", buildup)
                c2.metric("Trend Strength", _fmt_num(strength, decimals=1, suffix="%") if strength is not None else DATA_NA)
                c3.metric("AI Signal", signal)

                c4, c5, c6 = st.columns(3)
                c4.metric("Volume", _fmt_int(volume) if volume else DATA_NA)
                c5.metric("OI", _fmt_int(oi) if oi is not None else DATA_NA)
                c6.metric("Price %", _fmt_num(price_change_pct, suffix="%"))
                if have_oi_data:
                    st.caption(f"OI source: {oi_source} · OI Change: {_fmt_num(oi_change_pct, suffix='%')}")
                else:
                    st.caption(f"OI data unavailable from FYERS and NSE fallback for {fut_symbol}.")
            except Exception as exc:
                st.warning(f"Unable to analyze futures for {label} ({exc}). {DATA_NA}")


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

            with st.spinner(f"{FETCHING_MSG} {symbol}..."):
                a = _analyze_instrument(fyers, symbol)
            if a is None:
                st.warning(DATA_NA)
                continue

            try:
                e20_series = ema(a["df"]["close"], 20)
                e50_series = ema(a["df"]["close"], 50)
                golden_cross = len(a["df"]) >= 2 and a["e20"] > a["e50"] and e20_series.iloc[-2] <= e50_series.iloc[-2]
                death_cross = len(a["df"]) >= 2 and a["e20"] < a["e50"] and e20_series.iloc[-2] >= e50_series.iloc[-2]

                rel_vol = a["rel_vol"] if a["rel_vol"] is not None else 0.0
                vol_spike = rel_vol > 2.0
                institutional_buying = rel_vol > 1.5 and a["bullish_candle"]
                institutional_selling = rel_vol > 1.5 and not a["bullish_candle"]

                c1a, c2a, c3a, c4a = st.columns(4)
                c1a.metric("EMA20", _fmt_num(a["e20"]))
                c2a.metric("EMA50", _fmt_num(a["e50"]))
                c3a.metric("EMA200", _fmt_num(a["e200"]) if a["e200"] is not None else DATA_NA)
                c4a.metric("VWAP", _fmt_num(a["vwap"]))

                c5a, c6a, c7a, c8a = st.columns(4)
                c5a.metric("RSI", _fmt_num(a["rsi"], decimals=1))
                c6a.metric("MACD Hist", _fmt_num(a["macd_hist"].iloc[-1], decimals=3))
                c7a.metric("ADX", _fmt_num(a["adx"], decimals=1))
                c8a.metric("ATR", _fmt_num(a["atr"]))

                c9a, c10a = st.columns(2)
                c9a.metric("Support", _fmt_num(a["support"]))
                c10a.metric("Resistance", _fmt_num(a["resistance"]))

                tags = []
                if golden_cross:
                    tags.append("🟢 Golden Cross")
                if death_cross:
                    tags.append("🔴 Death Cross")
                if a["breakout"]:
                    tags.append("🚀 Breakout")
                if a["breakdown"]:
                    tags.append("⚠️ Breakdown")
                if vol_spike:
                    tags.append("📊 Volume Spike")
                if institutional_buying:
                    tags.append("🏦 Institutional Buying")
                if institutional_selling:
                    tags.append("🏦 Institutional Selling")

                st.write(f"Relative Volume: **{rel_vol}x**" if a["rel_vol"] is not None else f"Relative Volume: {DATA_NA}")
                st.write(" | ".join(tags) if tags else "No special patterns detected.")
            except Exception as exc:
                st.warning(f"Unable to compute indicators for {symbol} ({exc}). {DATA_NA}")


def render_smc_section(fyers: Any) -> None:
    """Section 10: Smart Money Concepts."""
    st.subheader("🧩 Smart Money Concepts (SMC)")
    watchlist = st.session_state.get("ai_watchlist", WATCHLIST_DEFAULT)
    options = [v for k, v in INDEX_SYMBOLS.items() if k != "INDIA VIX"] + list(watchlist)
    selected = st.selectbox("Select instrument for SMC analysis", options, key="smc_symbol_select")

    with st.spinner(f"{FETCHING_MSG} SMC data for {selected}..."):
        df = fetch_history_cached(fyers, selected, "15", 30)
    if df is None or df.empty:
        st.warning(DATA_NA)
        return

    smc = compute_smc(df)
    c1, c2 = st.columns(2)
    c1.metric("Swing High", _fmt_num(smc["swing_high"]) if smc["swing_high"] else DATA_NA)
    c2.metric("Swing Low", _fmt_num(smc["swing_low"]) if smc["swing_low"] else DATA_NA)

    c3, c4 = st.columns(2)
    c3.metric("BOS", smc["bos"])
    c4.metric("CHOCH", smc["choch"])

    st.write(f"**Order Block:** {smc['order_block']}")
    st.write(f"**Demand Zone:** {smc.get('demand_zone') or 'NONE'}")
    st.write(f"**Supply Zone:** {smc.get('supply_zone') or 'NONE'}")
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


def render_greeks_section(fyers: Any) -> None:
    """NEW (v3) Section: Options Greeks Engine."""
    st.subheader("🧮 Options Greeks Engine")
    options = [v for k, v in INDEX_SYMBOLS.items() if k != "INDIA VIX"]
    selected = st.selectbox("Select instrument for Greeks analysis", options, key="greeks_symbol_select")

    with st.spinner(f"{FETCHING_MSG} option chain greeks for {selected}..."):
        oc = _safe_call(fetch_option_chain_data, fyers, selected, default=dict(_EMPTY_OPTION_CHAIN_METRICS))

    if oc.get("chain_detail") is None:
        st.warning(f"{DATA_NA} - the FYERS option-chain response for this symbol did not include a 'greeks' payload.")
        return

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Avg Call Delta", _fmt_num(oc.get("avg_call_delta"), decimals=3) if oc.get("avg_call_delta") is not None else DATA_NA)
    c2.metric("Avg Put Delta", _fmt_num(oc.get("avg_put_delta"), decimals=3) if oc.get("avg_put_delta") is not None else DATA_NA)
    c3.metric("Avg Theta", _fmt_num(oc.get("avg_theta"), decimals=3) if oc.get("avg_theta") is not None else DATA_NA)
    c4.metric("Avg Vega", _fmt_num(oc.get("avg_vega"), decimals=3) if oc.get("avg_vega") is not None else DATA_NA)

    c5, c6, c7, c8 = st.columns(4)
    c5.metric("ATM IV", _fmt_num(oc.get("atm_iv"), suffix="%") if oc.get("atm_iv") is not None else DATA_NA)
    c6.metric("IV Rank (session)", _fmt_num(oc.get("iv_rank"), decimals=1, suffix="%") if oc.get("iv_rank") is not None else DATA_NA)
    c7.metric("IV Percentile (session)", _fmt_num(oc.get("iv_percentile"), decimals=1, suffix="%") if oc.get("iv_percentile") is not None else DATA_NA)
    c8.metric("Gamma Exposure", _fmt_num(oc.get("gamma_exposure"), decimals=4) if oc.get("gamma_exposure") is not None else DATA_NA)

    c9, c10 = st.columns(2)
    c9.metric("Delta Exposure", _fmt_num(oc.get("delta_exposure")) if oc.get("delta_exposure") is not None else DATA_NA)
    c10.metric("ATM Strike", _fmt_num(oc.get("atm_strike")) if oc.get("atm_strike") is not None else DATA_NA)

    st.caption(
        "IV Rank / IV Percentile are computed against ATM-IV observations made "
        "live during this session (no external historical-IV feed is wired in) - "
        "they become more meaningful as the session progresses, and start near "
        "50% with few observations. This is disclosed rather than presented as "
        "a full 252-day IV Rank."
    )

    if any(v.get("Gamma") is not None for v in oc.get("chain_detail", [])) or True:
        with st.expander("What High Gamma / High Vega / Theta Decay mean here"):
            st.write(
                "- **High Gamma** near the ATM strike means Delta (and therefore the "
                "position's directional exposure) can change quickly for a small "
                "move in the underlying - option prices become more reactive.\n"
                "- **High Vega** means the option's price is more sensitive to "
                "changes in implied volatility - a spike or crush in IV moves the "
                "premium a lot even if the underlying doesn't move.\n"
                "- **Theta Decay** is the daily erosion of an option's time value; "
                "it accelerates as expiry approaches, especially for ATM strikes.\n"
                "- **Premium Expansion** happens when IV Rank/Percentile is rising "
                "(often around events) - premiums richen independent of direction."
            )

    with st.expander("Per-Strike ATM / ITM / OTM Table"):
        detail = oc.get("chain_detail") or []
        if detail:
            st.dataframe(pd.DataFrame(detail), use_container_width=True, hide_index=True)
        else:
            st.info(DATA_NA)


def render_global_markets(fyers: Any) -> pd.DataFrame:
    """NEW (v3) Section: Global Markets. Returns the fetched DataFrame for reuse in scoring."""
    st.subheader("🌍 Global Markets")
    with st.spinner(f"{FETCHING_MSG} global market data..."):
        global_df = _safe_call(get_global_markets_summary, fyers, default=pd.DataFrame())
    if global_df.empty:
        st.warning(DATA_NA)
        return global_df

    for _, row in global_df.iterrows():
        c1, c2, c3, c4 = st.columns([2, 1, 1, 1])
        c1.write(f"**{row['Instrument']}**  \n_{row['Source']}_")
        c2.metric("Price", _fmt_num(row["Price"]) if row["Price"] is not None else DATA_NA)
        c3.metric("Change", _fmt_num(row["Change"]) if row["Change"] is not None else DATA_NA)
        c4.metric("Change %", _fmt_num(row["Change %"], suffix="%") if row["Change %"] is not None else DATA_NA)
    st.caption(
        "USDINR / Crude / Gold are sourced live from FYERS. Gift Nifty, US "
        "Futures, and Asian/European indices are not served by FYERS and use "
        "a best-effort external quote lookup - shown as Data Not Available if "
        "that lookup also fails."
    )
    return global_df


def render_true_breadth_section(fyers: Any) -> Dict[str, Any]:
    """NEW (v3) Section: True Market Breadth. Returns the breadth dict for reuse in scoring."""
    st.subheader("📊 True Market Breadth (NIFTY 50)")
    with st.spinner(f"{FETCHING_MSG} market breadth..."):
        breadth = _safe_call(compute_true_market_breadth, fyers, default={})
    if not breadth or breadth.get("advances") is None:
        st.warning(DATA_NA)
        return breadth or {}

    c1, c2, c3 = st.columns(3)
    c1.metric("Advances", breadth["advances"])
    c2.metric("Declines", breadth["declines"])
    c3.metric("Unchanged", breadth["unchanged"])

    col_a, col_b = st.columns(2)
    with col_a:
        st.markdown("**Top Gainers**")
        if not breadth["top_gainers"].empty:
            st.dataframe(breadth["top_gainers"][["Symbol", "LTP", "Change %"]], use_container_width=True, hide_index=True)
        else:
            st.info(DATA_NA)
    with col_b:
        st.markdown("**Top Losers**")
        if not breadth["top_losers"].empty:
            st.dataframe(breadth["top_losers"][["Symbol", "LTP", "Change %"]], use_container_width=True, hide_index=True)
        else:
            st.info(DATA_NA)

    st.markdown("**Volume Leaders**")
    if not breadth["volume_leaders"].empty:
        st.dataframe(breadth["volume_leaders"][["Symbol", "Volume", "Change %"]], use_container_width=True, hide_index=True)
    else:
        st.info(DATA_NA)

    st.markdown("**Heavyweight Contribution** (approx. weight × change %, ranked by |contribution|)")
    if not breadth["heavyweight_contribution"].empty:
        st.dataframe(
            breadth["heavyweight_contribution"][["Symbol", "Weight", "Change %", "Contribution"]],
            use_container_width=True, hide_index=True,
        )
    else:
        st.info(DATA_NA)

    st.markdown("**Sector Rotation**")
    sector_df = _safe_call(get_sector_rotation, fyers, default=pd.DataFrame())
    if not sector_df.empty:
        st.dataframe(sector_df, use_container_width=True, hide_index=True)
    else:
        st.info(DATA_NA)

    return breadth


def render_ai_prediction_section(fyers: Any, news_df: pd.DataFrame, fii_dii_net: float,
                                  vix_value: Optional[float], global_df: Optional[pd.DataFrame],
                                  true_breadth: Optional[Dict[str, Any]]) -> None:
    """Section 11: AI Prediction - full multi-factor confirmation engine."""
    st.subheader("🎯 AI Prediction")
    watchlist = st.session_state.get("ai_watchlist", WATCHLIST_DEFAULT)
    options = [v for k, v in INDEX_SYMBOLS.items() if k != "INDIA VIX"] + list(watchlist)
    selected = st.selectbox("Select instrument for AI prediction", options, key="prediction_symbol_select")

    with st.spinner(f"{FETCHING_MSG} prediction inputs for {selected}..."):
        a = _analyze_instrument(fyers, selected)
    if a is None:
        st.warning(DATA_NA)
        return

    try:
        avg_vol = a["df"]["volume"].tail(20).mean()
        rel_vol = a["rel_vol"]
        oi_change_pct = a["option_chain"].get("oi_change_pct") or 0.0
        price_change_pct_now = (
            (a["close"] - a["df"]["close"].iloc[-2]) / a["df"]["close"].iloc[-2] * 100 if len(a["df"]) > 1 else 0.0
        )
        buildup = classify_futures_buildup(price_change_pct_now, oi_change_pct)

        signals = InstrumentSignals(
            close=a["close"], e20=a["e20"], e50=a["e50"], e200=a["e200"], vwap_val=a["vwap"],
            rsi_val=a["rsi"], macd_hist=float(a["macd_hist"].iloc[-1]), adx_val=a["adx"], atr_val=a["atr"],
            supertrend_dir=a["supertrend_dir"], support=a["support"], resistance=a["resistance"],
            smc=a["smc"], option_chain=a["option_chain"], rel_vol=rel_vol,
            bullish_candle=a["bullish_candle"], buildup=buildup,
        )
        direction = evaluate_instrument_direction(signals, news_df, fii_dii_net, vix_value)

        # --- Professional cross-checks (v3) ---------------------------
        greeks_s = score_greeks(a["option_chain"])
        global_s = score_global_markets(global_df)
        breadth_s = score_true_breadth(true_breadth or {})

        spot_positive = price_change_pct_now > 0
        fut_symbol_guess = None
        for label, underlying in FUTURES_UNDERLYINGS.items():
            if underlying in selected:
                fut_symbol_guess = _current_month_future_symbol(underlying)
                break
        fut_q = _safe_call(fetch_quote, fyers, fut_symbol_guess, default=None) if fut_symbol_guess else None
        fut_positive = fut_q.get("chp") > 0 if (fut_q and fut_q.get("chp") is not None) else None

        futures_vs_spot_agree = check_agreement(spot_positive, fut_positive) if fut_positive is not None else None

        oc_positive = None
        pcr = a["option_chain"].get("pcr")
        if pcr is not None:
            oc_positive = pcr > 1.0
        option_chain_vs_futures_agree = check_agreement(oc_positive, fut_positive)

        greeks_positive = None
        call_d, put_d = a["option_chain"].get("avg_call_delta"), a["option_chain"].get("avg_put_delta")
        if call_d is not None and put_d is not None:
            greeks_positive = (call_d + put_d) > 0
        oi_positive = None
        delta_oi = a["option_chain"].get("delta_oi")
        if delta_oi is not None:
            oi_positive = delta_oi < 0  # more Call OI than Put OI -> resistance-side heavy -> treat >CE as bearish bias proxy is debatable; use put_writing instead
        greeks_vs_oi_agree = check_agreement(greeks_positive, oi_positive)

        direction = apply_professional_rules(
            direction, greeks_s, global_s, breadth_s,
            futures_vs_spot_agree=futures_vs_spot_agree,
            option_chain_vs_futures_agree=option_chain_vs_futures_agree,
            greeks_vs_oi_agree=greeks_vs_oi_agree,
        )

        atr_val = a["atr"] if a["atr"] and a["atr"] > 0 else a["close"] * 0.005
        net = direction["net_score"]
        trade_direction = 1 if net >= 0 else -1
        target1 = round(a["close"] + trade_direction * atr_val * 1.5, 2)
        target2 = round(a["close"] + trade_direction * atr_val * 3.0, 2)
        stoploss = round(a["close"] - trade_direction * atr_val * 1.0, 2)
        risk = abs(a["close"] - stoploss)
        reward = abs(target1 - a["close"])
        rr = round(reward / risk, 2) if risk > 0 else 0.0

        rec_colors = {"STRONG BUY": "🟢", "BUY": "🟢", "WAIT": "🟡", "SELL": "🔴", "STRONG SELL": "🔴"}
        st.markdown(f"### {rec_colors.get(direction['recommendation'], '⚪')} {direction['recommendation']}")
        st.caption(
            f"Market Direction: {direction['direction']} · "
            f"Confirmations: {direction['confirmations']}/{direction['confirmations_required']} required "
            "(Trend, SMC, Option Chain, News, Institutional Flow, Momentum)"
        )
        for note in direction.get("professional_notes", []):
            st.caption(f"ℹ️ {note}")
        if direction["recommendation"] == "WAIT":
            st.info(
                "Confirmation threshold not met, or a professional cross-check "
                "(Futures vs Spot, Option Chain vs Futures, Greeks vs OI) disagreed - "
                "showing WAIT to avoid a false signal."
            )

        c1, c2, c3 = st.columns(3)
        c1.metric("Bullish Score %", f"{direction['bullish_score']}%")
        c2.metric("Bearish Score %", f"{direction['bearish_score']}%")
        c3.metric("Confidence %", f"{direction['confidence']}%")

        c4, c5, c6 = st.columns(3)
        c4.metric("Probability Up %", f"{direction['prob_up']}%")
        c5.metric("Probability Down %", f"{direction['prob_down']}%")
        c6.metric("Risk:Reward", f"1:{rr}")

        c7, c8, c9 = st.columns(3)
        c7.metric("Target 1", _fmt_num(target1))
        c8.metric("Target 2", _fmt_num(target2))
        c9.metric("Stop Loss", _fmt_num(stoploss))

        st.progress(min(1.0, max(0.0, direction["confidence"] / 100)))

        with st.expander("Factor Breakdown"):
            comp_df = pd.DataFrame(
                [{"Factor": k.replace("_", " ").title(), "Weight %": DIRECTION_WEIGHTS[k] * 100,
                  "Score (-100..100)": direction["components"].get(k, 0.0)} for k in DIRECTION_WEIGHTS]
            )
            st.dataframe(comp_df, use_container_width=True, hide_index=True)
            st.caption(
                f"Greeks (informational): {direction.get('greeks_score', DATA_NA)} · "
                f"Global Markets (informational): {direction.get('global_score', DATA_NA)} · "
                f"True Breadth (informational): {direction.get('breadth_score', DATA_NA)}"
            )
    except Exception as exc:
        st.warning(f"Unable to generate AI prediction for {selected} ({exc}). {DATA_NA}")


# ==============================================================================
# 12. MAIN ENTRY POINT
# ==============================================================================

def show_ai_market_intelligence(fyers: Any) -> None:
    """
    Public entry point. Renders the complete AI Market Intelligence dashboard.

    Call this from market.py:

        from ai_market_intelligence import show_ai_market_intelligence
        show_ai_market_intelligence(fyers)

    This function is fully self-contained, never raises, and does not modify
    scanner.py, option_chain.py, app.py, market.py, trading.py, or any
    existing BUY/SELL signal logic.
    """
    st.markdown("## 🧠 AI Market Intelligence")
    st.caption(f"Last updated: {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} · Auto-refresh: 30s")

    if _HAS_AUTOREFRESH:
        st_autorefresh(interval=REFRESH_INTERVAL_MS, key="ai_market_intel_autorefresh")
    else:
        st.caption("Install `streamlit-autorefresh` to enable automatic 30-second refresh.")

    with st.spinner(f"{FETCHING_MSG} live market data..."):
        market_df = _safe_call(get_market_summary, fyers, INDEX_SYMBOLS, default=pd.DataFrame())

    vix_value: Optional[float] = None
    if not market_df.empty:
        vix_row = market_df[market_df["Index"] == "INDIA VIX"]
        if not vix_row.empty and pd.notna(vix_row["LTP"].iloc[0]):
            vix_value = float(vix_row["LTP"].iloc[0])

    with st.spinner(f"{FETCHING_MSG} latest news..."):
        news_df = _safe_call(fetch_all_news, default=pd.DataFrame())

    fii_dii_data = _safe_call(fetch_fii_dii, default=None)
    fii_dii_net = 0.0
    if fii_dii_data:
        fii_dii_net = (fii_dii_data["fii_buy"] - fii_dii_data["fii_sell"]) - (fii_dii_data["dii_buy"] - fii_dii_data["dii_sell"])

    breadth = compute_market_breadth(market_df)  # legacy 6-index proxy (kept for compatibility)

    with st.spinner(f"{FETCHING_MSG} global markets..."):
        global_df = _safe_call(get_global_markets_summary, fyers, default=pd.DataFrame())

    with st.spinner(f"{FETCHING_MSG} true market breadth..."):
        true_breadth = _safe_call(compute_true_market_breadth, fyers, default={})

    # Overall AI Market Direction: NIFTY is used as the primary composite
    # instrument (all seven weighted factors), which is far more reliable
    # than any single indicator such as PCR, News, or OI alone.
    with st.spinner(f"{FETCHING_MSG} AI direction inputs..."):
        nifty_analysis = _analyze_instrument(fyers, INDEX_SYMBOLS["NIFTY"])

    if nifty_analysis is not None:
        oi_change_pct = nifty_analysis["option_chain"].get("oi_change_pct") or 0.0
        prev_close_series = nifty_analysis["df"]["close"]
        price_change_pct = (
            (nifty_analysis["close"] - prev_close_series.iloc[-2]) / prev_close_series.iloc[-2] * 100
            if len(prev_close_series) > 1 else 0.0
        )
        buildup = classify_futures_buildup(price_change_pct, oi_change_pct)
        signals = InstrumentSignals(
            close=nifty_analysis["close"], e20=nifty_analysis["e20"], e50=nifty_analysis["e50"],
            e200=nifty_analysis["e200"], vwap_val=nifty_analysis["vwap"], rsi_val=nifty_analysis["rsi"],
            macd_hist=float(nifty_analysis["macd_hist"].iloc[-1]), adx_val=nifty_analysis["adx"],
            atr_val=nifty_analysis["atr"], supertrend_dir=nifty_analysis["supertrend_dir"],
            support=nifty_analysis["support"], resistance=nifty_analysis["resistance"],
            smc=nifty_analysis["smc"], option_chain=nifty_analysis["option_chain"],
            rel_vol=nifty_analysis["rel_vol"], bullish_candle=nifty_analysis["bullish_candle"], buildup=buildup,
        )
        direction = evaluate_instrument_direction(signals, news_df, fii_dii_net, vix_value)

        greeks_s = score_greeks(nifty_analysis["option_chain"])
        global_s = score_global_markets(global_df)
        breadth_s = score_true_breadth(true_breadth)

        nifty_fut_q = _safe_call(fetch_quote, fyers, _current_month_future_symbol("NIFTY"), default=None)
        futures_vs_spot_agree = None
        if nifty_fut_q and nifty_fut_q.get("chp") is not None:
            futures_vs_spot_agree = check_agreement(price_change_pct > 0, nifty_fut_q.get("chp") > 0)

        pcr = nifty_analysis["option_chain"].get("pcr")
        option_chain_vs_futures_agree = None
        if pcr is not None and nifty_fut_q and nifty_fut_q.get("chp") is not None:
            option_chain_vs_futures_agree = check_agreement(pcr > 1.0, nifty_fut_q.get("chp") > 0)

        call_d = nifty_analysis["option_chain"].get("avg_call_delta")
        put_d = nifty_analysis["option_chain"].get("avg_put_delta")
        delta_oi = nifty_analysis["option_chain"].get("delta_oi")
        greeks_vs_oi_agree = None
        if call_d is not None and put_d is not None and delta_oi is not None:
            greeks_vs_oi_agree = check_agreement((call_d + put_d) > 0, delta_oi > 0)

        direction = apply_professional_rules(
            direction, greeks_s, global_s, breadth_s,
            futures_vs_spot_agree=futures_vs_spot_agree,
            option_chain_vs_futures_agree=option_chain_vs_futures_agree,
            greeks_vs_oi_agree=greeks_vs_oi_agree,
        )
    else:
        direction = compute_weighted_direction(0, 0, 0, score_news(news_df), 0, 0, 0, vix_value=vix_value)
        direction["greeks_score"] = 0.0
        direction["global_score"] = score_global_markets(global_df)
        direction["breadth_score"] = score_true_breadth(true_breadth)

    tabs = st.tabs([
        "🧠 AI Dashboard", "📊 Market Summary", "📈 India VIX", "📰 Live News",
        "🤖 News Analysis", "🏦 FII/DII", "📐 Index Analysis", "📉 Futures",
        "📌 Stocks", "🧩 SMC", "🧮 Greeks", "🌍 Global Markets", "📊 True Breadth",
        "🎯 AI Prediction",
    ])

    with tabs[0]:
        _render_safely("AI Dashboard", render_ai_dashboard, direction, breadth, true_breadth)
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
        _render_safely("Options Greeks", render_greeks_section, fyers)
    with tabs[11]:
        _render_safely("Global Markets", render_global_markets, fyers)
    with tabs[12]:
        _render_safely("True Market Breadth", render_true_breadth_section, fyers)
    with tabs[13]:
        _render_safely(
            "AI Prediction", render_ai_prediction_section, fyers, news_df, fii_dii_net,
            vix_value, global_df, true_breadth,
        )
