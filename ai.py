"""
================================================================================
 F&O TOP MOVERS BUY/SELL ENGINE
 Institutional-style F&O AI Decision Engine for NSE Futures & Options stocks.
================================================================================

WHAT THIS IS
------------
A self-contained Streamlit application that scans the NSE F&O universe and
ranks stocks using a multi-factor ensemble of price-action / market-structure
(Smart Money Concepts), classical technical indicators, options-chain
analytics (PCR, OI, IV, Max Pain, Greeks), volatility/breadth context
(India VIX, sector & index strength) and a gradient-boosted (XGBoost)
probability model, then produces a full institutional-style trade plan
(equity + options) for the Top 10 BUY and Top 10 SELL candidates.

HONEST DATA DISCLAIMER (read before deploying)
------------------------------------------------
Genuine "institutional order-flow" data (custodial/FII block-deal ledgers,
broker proprietary flow, etc.) is not publicly available in real time.
Every score in this engine that is labelled "Institutional", "Smart Money",
"Delivery", "OI", "PCR" etc. is computed from *publicly available* market
data (price/volume/delivery %, NSE option-chain JSON, India VIX, index
data) using well-established retail/quant proxies for institutional
activity (delivery-% spikes, OI build-up direction, relative volume,
IV skew, etc.). It is NOT a guarantee of real institutional positioning.
This tool is for research/education. It is not investment advice, and
"Expected ROI" / "Expected Accuracy" are model estimates, not promises.
Always do your own due diligence and manage risk (position sizing / stop
loss) independently before trading.

RUNTIME / NETWORK NOTE
-----------------------
This script fetches live data over the internet (yfinance, NSE India
option-chain endpoint, India VIX). It is written to run in a normal
internet-connected environment (`streamlit run ai.py`). Every network
call is wrapped in defensive try/except with graceful fallbacks so a
single symbol's failed fetch (rate limit, holiday, delisted, no option
chain, etc.) never crashes the scan -- that row is simply scored with
whatever data is available and marked accordingly in AI Remarks.

Run:
    pip install streamlit yfinance pandas numpy requests scipy xgboost \
                openpyxl streamlit-autorefresh
    streamlit run ai.py
================================================================================
"""

from __future__ import annotations

import io
import json
import math
import time
import warnings
import datetime as dt
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd
import requests
import streamlit as st

warnings.filterwarnings("ignore")

# ------------------------------------------------------------------------- #
# Optional third-party dependencies (engine must never crash if missing)
# ------------------------------------------------------------------------- #
try:
    import yfinance as yf
    YF_AVAILABLE = True
except Exception:
    YF_AVAILABLE = False

try:
    from scipy.stats import norm
    SCIPY_AVAILABLE = True
except Exception:
    SCIPY_AVAILABLE = False

try:
    import xgboost as xgb
    XGB_AVAILABLE = True
except Exception:
    XGB_AVAILABLE = False

try:
    from streamlit_autorefresh import st_autorefresh
    AUTOREFRESH_AVAILABLE = True
except Exception:
    AUTOREFRESH_AVAILABLE = False


# ============================================================================
# SECTION 1 : CONFIG / CONSTANTS
# ============================================================================

APP_TITLE = "F&O TOP MOVERS BUY/SELL ENGINE"
RISK_FREE_RATE = 0.065          # approx Indian T-bill rate, used in Greeks
DEFAULT_CAPITAL = 100000.0
DEFAULT_RISK_PCT = 1.0          # % of capital risked per trade (equity plan)
HIST_PERIOD = "1y"
HIST_INTERVAL = "1d"

# NSE F&O stock universe (representative, liquid F&O names). This is used
# as the scan universe and as a safe fallback if the live NSE F&O symbol
# list endpoint cannot be reached.
FNO_SYMBOLS = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "SBIN", "AXISBANK",
    "KOTAKBANK", "HINDUNILVR", "ITC", "BHARTIARTL", "LT", "BAJFINANCE",
    "BAJAJFINSV", "ASIANPAINT", "MARUTI", "TITAN", "SUNPHARMA", "ULTRACEMCO",
    "NESTLEIND", "WIPRO", "HCLTECH", "TECHM", "ADANIENT", "ADANIPORTS",
    "ADANIGREEN", "ADANIPOWER", "TATASTEEL", "TATAMOTORS", "TATACONSUM",
    "TATAPOWER", "JSWSTEEL", "HINDALCO", "COALINDIA", "NTPC", "POWERGRID",
    "ONGC", "BPCL", "IOC", "GRASIM", "CIPLA", "DRREDDY", "DIVISLAB",
    "APOLLOHOSP", "EICHERMOT", "HEROMOTOCO", "BAJAJ-AUTO", "M&M", "UPL",
    "SBILIFE", "HDFCLIFE", "ICICIPRULI", "ICICIGI", "BAJAJHLDNG",
    "SHREECEM", "AMBUJACEM", "ACC", "DLF", "GODREJPROP", "OBEROIRLTY",
    "PIDILITIND", "BERGEPAINT", "HAVELLS", "VOLTAS", "DIXON", "POLYCAB",
    "SIEMENS", "ABB", "CUMMINSIND", "BEL", "HAL", "BHEL", "IRCTC",
    "INDIGO", "ZOMATO", "NYKAA", "PAYTM", "POLICYBZR", "PVRINOX",
    "TRENT", "DMART", "JUBLFOOD", "MCDOWELL-N", "COLPAL", "MARICO",
    "DABUR", "GODREJCP", "BRITANNIA", "PGHH", "LTIM", "PERSISTENT",
    "COFORGE", "MPHASIS", "LTTS", "OFSS", "NAUKRI", "INDUSINDBK",
    "FEDERALBNK", "BANDHANBNK", "IDFCFIRSTB", "PNB", "BANKBARODA",
    "CANBK", "AUBANK", "RBLBANK", "CHOLAFIN", "MUTHOOTFIN", "PEL",
    "LICHSGFIN", "PFC", "RECLTD", "IRFC", "SAIL", "NMDC", "VEDL",
    "JINDALSTEL", "NATIONALUM", "HINDCOPPER", "GAIL", "PETRONET",
    "IGL", "MGL", "GUJGASLTD", "CONCOR", "CROMPTON", "WHIRLPOOL",
    "BLUESTARCO", "AMBER", "KEI", "RVNL", "IRCON", "NBCC", "NCC",
    "GMRAIRPORT", "ASHOKLEY", "TVSMOTOR", "BALKRISIND", "MRF", "APOLLOTYRE",
    "BOSCHLTD", "EXIDEIND", "MOTHERSON", "BHARATFORG", "SONACOMS",
    "ESCORTS", "CANFINHOME", "SUNTV", "ZEEL", "PVR", "DELTACORP",
    "LUPIN", "AUROPHARMA", "ALKEM", "TORNTPHARM", "GLENMARK", "BIOCON",
    "LAURUSLABS", "GRANULES", "SYNGENE", "MFSL", "MANAPPURAM",
    "ABCAPITAL", "ABFRL", "PAGEIND", "RELAXO", "BATAINDIA", "ASTRAL",
    "SUPREMEIND", "APLAPOLLO", "JSL", "RAMCOCEM", "JKCEMENT",
    "IEX", "CDSL", "BSE", "MCX", "ANGELONE", "IIFL", "HDFCAMC",
    "UTIAMC", "NAM-INDIA", "STAR", "METROPOLIS", "LALPATHLAB",
    "FORTIS", "MAXHEALTH", "GLAND", "NAVINFLUOR", "SRF", "DEEPAKNTR",
    "AARTIIND", "PIIND", "GNFC", "CHAMBLFERT", "COROMANDEL",
]
FNO_SYMBOLS = sorted(set(FNO_SYMBOLS))

# Approximate NSE F&O market lot sizes for the most liquid names (as
# publicly notified by NSE; these are periodically revised by the
# exchange, so treat as approximate / update per the latest circular).
KNOWN_LOT_SIZES = {
    "RELIANCE": 500, "TCS": 175, "HDFCBANK": 550, "INFY": 400,
    "ICICIBANK": 700, "SBIN": 1500, "AXISBANK": 625, "KOTAKBANK": 400,
    "HINDUNILVR": 300, "ITC": 1600, "BHARTIARTL": 475, "LT": 275,
    "BAJFINANCE": 125, "BAJAJFINSV": 500, "ASIANPAINT": 200,
    "MARUTI": 100, "TITAN": 375, "SUNPHARMA": 700, "ULTRACEMCO": 100,
    "NESTLEIND": 250, "WIPRO": 3000, "HCLTECH": 700, "TECHM": 600,
    "ADANIENT": 250, "ADANIPORTS": 800, "TATASTEEL": 4250,
    "TATAMOTORS": 1425, "JSWSTEEL": 1350, "HINDALCO": 1400,
    "COALINDIA": 2100, "NTPC": 3000, "POWERGRID": 2700, "ONGC": 3850,
    "BPCL": 1800, "GRASIM": 475, "CIPLA": 650, "DRREDDY": 125,
    "APOLLOHOSP": 125, "EICHERMOT": 175, "HEROMOTOCO": 300,
    "M&M": 350, "SBILIFE": 750, "HDFCLIFE": 1100, "DLF": 1650,
    "INDIGO": 300, "ZOMATO": 3425, "PAYTM": 800, "TRENT": 100,
    "DMART": 200, "INDUSINDBK": 700, "FEDERALBNK": 5000, "PNB": 8000,
    "BANKBARODA": 2925, "CANBK": 6750, "SAIL": 6500, "VEDL": 1600,
    "GAIL": 3450, "TATAPOWER": 2850,
}
DEFAULT_LOT_SIZE_NOTIONAL = 500000  # NSE targets ~5-10L notional per lot

# ============================================================================
# SECTION 2 : SAFE UTILITIES  (NaN-safe everywhere)
# ============================================================================

def safe_float(x, default: float = 0.0) -> float:
    try:
        if x is None:
            return default
        f = float(x)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except Exception:
        return default


def safe_round(x, digits: int = 2, default: float = 0.0) -> float:
    return round(safe_float(x, default), digits)


def clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def pct(part: float, whole: float, default: float = 0.0) -> float:
    whole = safe_float(whole)
    if whole == 0:
        return default
    return safe_float(part) / whole * 100.0


# ============================================================================
# SECTION 3 : DATA FETCH LAYER  (all defensive, all cached)
# ============================================================================

@st.cache_data(ttl=300, show_spinner=False)
def fetch_price_history(symbol: str) -> Optional[pd.DataFrame]:
    """OHLCV history for a symbol via yfinance (.NS). Never raises."""
    if not YF_AVAILABLE:
        return None
    try:
        tk = yf.Ticker(f"{symbol}.NS")
        df = tk.history(period=HIST_PERIOD, interval=HIST_INTERVAL, auto_adjust=False)
        if df is None or df.empty or len(df) < 30:
            return None
        df = df.rename(columns=str.title)
        df.index = pd.to_datetime(df.index)
        return df.dropna(how="all")
    except Exception:
        return None


@st.cache_data(ttl=300, show_spinner=False)
def fetch_delivery_pct(symbol: str) -> float:
    """
    Best-effort delivery % proxy. NSE's official delivery-position bhavcopy
    requires an authenticated/session-cookie CSV pull that is unreliable
    outside a browser session, so we fall back to a volume-based delivery
    proxy (this is clearly a proxy, not the official NSE delivery figure).
    """
    try:
        df = fetch_price_history(symbol)
        if df is None or "Volume" not in df.columns:
            return float("nan")
        vol = df["Volume"].tail(20)
        if vol.empty or vol.mean() == 0:
            return float("nan")
        # proxy: inverse of volume volatility -> steadier volume => assumed
        # higher delivery-based (non-speculative) participation
        cv = safe_float(vol.std() / vol.mean(), 1.0)
        proxy = clip(70 - cv * 40, 15, 85)
        return round(proxy, 2)
    except Exception:
        return float("nan")


@st.cache_data(ttl=180, show_spinner=False)
def fetch_option_chain(symbol: str) -> Optional[dict]:
    """
    NSE India public option-chain JSON endpoint. Requires a warmed-up
    session (cookies from the base site) to avoid 401s. Returns None on
    any failure -- callers must handle a missing option chain gracefully.
    """
    try:
        headers = {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/124.0 Safari/537.36"),
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "*/*",
        }
        session = requests.Session()
        session.headers.update(headers)
        session.get("https://www.nseindia.com", timeout=5)
        url = f"https://www.nseindia.com/api/option-chain-equities?symbol={symbol}"
        resp = session.get(url, timeout=6)
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not data or "records" not in data:
            return None
        return data
    except Exception:
        return None


@st.cache_data(ttl=300, show_spinner=False)
def fetch_india_vix() -> float:
    if not YF_AVAILABLE:
        return float("nan")
    try:
        tk = yf.Ticker("^INDIAVIX")
        df = tk.history(period="5d")
        if df is None or df.empty:
            return float("nan")
        return safe_float(df["Close"].iloc[-1])
    except Exception:
        return float("nan")


@st.cache_data(ttl=300, show_spinner=False)
def fetch_index_series(ticker: str = "^NSEI") -> Optional[pd.DataFrame]:
    if not YF_AVAILABLE:
        return None
    try:
        tk = yf.Ticker(ticker)
        df = tk.history(period=HIST_PERIOD, interval=HIST_INTERVAL)
        if df is None or df.empty:
            return None
        return df
    except Exception:
        return None


@st.cache_data(ttl=900, show_spinner=False)
def fetch_fii_dii() -> dict:
    """
    Best-effort FII/DII net-flow snapshot. No stable free public JSON API
    exists for this in real time, so this defensively returns NaN/neutral
    if unavailable rather than fabricating numbers.
    """
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(
            "https://www.nseindia.com/api/fiidiiTradeReact",
            headers=headers, timeout=5,
        )
        if r.status_code != 200:
            return {"fii_net": float("nan"), "dii_net": float("nan")}
        data = r.json()
        fii_net = safe_float(data[0].get("fiiSell") if data else None)
        return {"fii_net": float("nan"), "dii_net": float("nan"), "raw": data}
    except Exception:
        return {"fii_net": float("nan"), "dii_net": float("nan")}


@st.cache_data(ttl=900, show_spinner=False)
def fetch_news_sentiment(symbol: str) -> float:
    """
    Lightweight keyword-based headline sentiment proxy in [-1, 1].
    Returns 0.0 (neutral) if no headlines can be retrieved -- never
    fabricates sentiment.
    """
    positive_kw = ["upgrade", "beats", "surge", "record", "growth", "buy",
                   "outperform", "rally", "profit rise", "strong", "wins",
                   "expansion", "bullish"]
    negative_kw = ["downgrade", "misses", "plunge", "loss", "sell", "probe",
                   "weak", "decline", "cut", "bearish", "fraud", "default"]
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        url = f"https://news.google.com/rss/search?q={symbol}+NSE+stock&hl=en-IN&gl=IN"
        r = requests.get(url, headers=headers, timeout=5)
        if r.status_code != 200:
            return 0.0
        text = r.text.lower()
        pos = sum(text.count(k) for k in positive_kw)
        neg = sum(text.count(k) for k in negative_kw)
        total = pos + neg
        if total == 0:
            return 0.0
        return round(clip((pos - neg) / total, -1, 1), 3)
    except Exception:
        return 0.0


# ============================================================================
# SECTION 4 : TECHNICAL INDICATOR ENGINE
# ============================================================================

def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.fillna(50)


def macd(series: pd.Series, fast=12, slow=26, signal=9):
    macd_line = ema(series, fast) - ema(series, slow)
    signal_line = ema(macd_line, signal)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low),
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0):
    hl2 = (df["High"] + df["Low"]) / 2
    atr_val = atr(df, period)
    upperband = hl2 + multiplier * atr_val
    lowerband = hl2 - multiplier * atr_val
    st_line = pd.Series(index=df.index, dtype=float)
    direction = pd.Series(index=df.index, dtype=int)
    st_line.iloc[0] = upperband.iloc[0]
    direction.iloc[0] = 1
    for i in range(1, len(df)):
        close = df["Close"].iloc[i]
        if close > upperband.iloc[i - 1]:
            direction.iloc[i] = 1
        elif close < lowerband.iloc[i - 1]:
            direction.iloc[i] = -1
        else:
            direction.iloc[i] = direction.iloc[i - 1]
            if direction.iloc[i] == 1 and lowerband.iloc[i] < lowerband.iloc[i - 1]:
                lowerband.iloc[i] = lowerband.iloc[i - 1]
            if direction.iloc[i] == -1 and upperband.iloc[i] > upperband.iloc[i - 1]:
                upperband.iloc[i] = upperband.iloc[i - 1]
        st_line.iloc[i] = lowerband.iloc[i] if direction.iloc[i] == 1 else upperband.iloc[i]
    return st_line, direction


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["High"], df["Low"], df["Close"]
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    tr = atr(df, period)
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / period, adjust=False).mean() / tr.replace(0, np.nan)
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / period, adjust=False).mean() / tr.replace(0, np.nan)
    dx = (abs(plus_di - minus_di) / (plus_di + minus_di).replace(0, np.nan)) * 100
    return dx.ewm(alpha=1 / period, adjust=False).mean().fillna(0)


def bollinger_bands(series: pd.Series, period: int = 20, std_mult: float = 2.0):
    mid = series.rolling(period).mean()
    std = series.rolling(period).std()
    upper = mid + std_mult * std
    lower = mid - std_mult * std
    return upper, mid, lower


def vwap(df: pd.DataFrame) -> pd.Series:
    typical = (df["High"] + df["Low"] + df["Close"]) / 3
    cum_vol = df["Volume"].cumsum().replace(0, np.nan)
    return (typical * df["Volume"]).cumsum() / cum_vol


def relative_volume(df: pd.DataFrame, lookback: int = 20) -> float:
    if len(df) < lookback + 1:
        return 1.0
    avg_vol = df["Volume"].tail(lookback + 1).iloc[:-1].mean()
    last_vol = df["Volume"].iloc[-1]
    if avg_vol == 0 or np.isnan(avg_vol):
        return 1.0
    return safe_float(last_vol / avg_vol, 1.0)


# ============================================================================
# SECTION 5 : SMART MONEY CONCEPTS (SMC) ENGINE
#   Market Structure, BOS, CHOCH, Order Blocks, Breaker Blocks, FVG,
#   Liquidity Sweeps -- implemented via swing-point price action logic.
# ============================================================================

def find_swing_points(df: pd.DataFrame, window: int = 3):
    highs, lows = df["High"], df["Low"]
    swing_high_idx, swing_low_idx = [], []
    for i in range(window, len(df) - window):
        seg_h = highs.iloc[i - window:i + window + 1]
        seg_l = lows.iloc[i - window:i + window + 1]
        if highs.iloc[i] == seg_h.max():
            swing_high_idx.append(i)
        if lows.iloc[i] == seg_l.min():
            swing_low_idx.append(i)
    return swing_high_idx, swing_low_idx


def market_structure_analysis(df: pd.DataFrame) -> dict:
    """
    Returns a dict describing structure trend, most recent BOS/CHOCH event,
    active order block, breaker block, fair value gap and liquidity sweep.
    """
    result = {
        "structure": "Sideways",
        "bos": "None",
        "choch": "None",
        "order_block": "None",
        "breaker_block": "None",
        "fvg": "None",
        "liquidity_sweep": "None",
    }
    try:
        sh_idx, sl_idx = find_swing_points(df, window=3)
        if len(sh_idx) < 2 or len(sl_idx) < 2:
            return result

        recent_highs = [df["High"].iloc[i] for i in sh_idx[-3:]]
        recent_lows = [df["Low"].iloc[i] for i in sl_idx[-3:]]

        higher_highs = len(recent_highs) >= 2 and recent_highs[-1] > recent_highs[-2]
        higher_lows = len(recent_lows) >= 2 and recent_lows[-1] > recent_lows[-2]
        lower_highs = len(recent_highs) >= 2 and recent_highs[-1] < recent_highs[-2]
        lower_lows = len(recent_lows) >= 2 and recent_lows[-1] < recent_lows[-2]

        if higher_highs and higher_lows:
            result["structure"] = "Bullish"
        elif lower_highs and lower_lows:
            result["structure"] = "Bearish"
        else:
            result["structure"] = "Sideways"

        last_close = df["Close"].iloc[-1]
        last_sh = df["High"].iloc[sh_idx[-1]]
        last_sl = df["Low"].iloc[sl_idx[-1]]

        # BOS: close breaks beyond the most recent swing high/low in the
        # direction of the prevailing structure.
        if result["structure"] == "Bullish" and last_close > last_sh:
            result["bos"] = "Bullish BOS"
        elif result["structure"] == "Bearish" and last_close < last_sl:
            result["bos"] = "Bearish BOS"

        # CHOCH: close breaks the most recent swing point *against* the
        # prevailing structure -> first sign of a character change.
        if result["structure"] == "Bullish" and last_close < last_sl:
            result["choch"] = "Bearish CHOCH"
        elif result["structure"] == "Bearish" and last_close > last_sh:
            result["choch"] = "Bullish CHOCH"

        # Order block: last down-candle before an up impulse (bullish OB)
        # or last up-candle before a down impulse (bearish OB), near
        # current price.
        closes = df["Close"]
        opens = df["Open"]
        ob_type, ob_level = "None", None
        for i in range(len(df) - 2, max(len(df) - 25, 5), -1):
            bullish_impulse = closes.iloc[i + 1] > opens.iloc[i + 1] and \
                (closes.iloc[i + 1] - opens.iloc[i + 1]) > atr(df).iloc[i + 1] * 0.8
            bearish_impulse = closes.iloc[i + 1] < opens.iloc[i + 1] and \
                (opens.iloc[i + 1] - closes.iloc[i + 1]) > atr(df).iloc[i + 1] * 0.8
            if bullish_impulse and closes.iloc[i] < opens.iloc[i]:
                ob_type, ob_level = "Bullish OB", df["Low"].iloc[i]
                break
            if bearish_impulse and closes.iloc[i] > opens.iloc[i]:
                ob_type, ob_level = "Bearish OB", df["High"].iloc[i]
                break
        if ob_level is not None:
            result["order_block"] = f"{ob_type} @ {safe_round(ob_level)}"

        # Breaker block: a former order block that price has broken
        # through and is now expected to act as the opposite role.
        if result["choch"] != "None" and ob_level is not None:
            breaker_type = "Bullish Breaker" if "Bullish" in result["choch"] else "Bearish Breaker"
            result["breaker_block"] = f"{breaker_type} @ {safe_round(ob_level)}"

        # Fair Value Gap: 3-candle imbalance where candle1 high < candle3 low
        # (bullish FVG) or candle1 low > candle3 high (bearish FVG), in the
        # last 10 candles.
        for i in range(len(df) - 3, max(len(df) - 12, 0), -1):
            c1_high, c1_low = df["High"].iloc[i], df["Low"].iloc[i]
            c3_high, c3_low = df["High"].iloc[i + 2], df["Low"].iloc[i + 2]
            if c1_high < c3_low:
                result["fvg"] = f"Bullish FVG {safe_round(c1_high)}-{safe_round(c3_low)}"
                break
            if c1_low > c3_high:
                result["fvg"] = f"Bearish FVG {safe_round(c3_high)}-{safe_round(c1_low)}"
                break

        # Liquidity sweep: a wick pokes beyond a recent swing point then
        # closes back inside it (stop-hunt behaviour).
        last_bar = df.iloc[-1]
        if len(sh_idx) >= 1 and last_bar["High"] > df["High"].iloc[sh_idx[-1]] and last_bar["Close"] < df["High"].iloc[sh_idx[-1]]:
            result["liquidity_sweep"] = "Sell-side Sweep (High swept)"
        elif len(sl_idx) >= 1 and last_bar["Low"] < df["Low"].iloc[sl_idx[-1]] and last_bar["Close"] > df["Low"].iloc[sl_idx[-1]]:
            result["liquidity_sweep"] = "Buy-side Sweep (Low swept)"

    except Exception:
        pass
    return result


# ============================================================================
# SECTION 6 : OPTION-CHAIN ANALYTICS  (PCR, OI, IV, Max Pain, Greeks)
# ============================================================================

def black_scholes_greeks(spot, strike, t_years, iv, r=RISK_FREE_RATE, opt_type="CE"):
    """Returns dict of delta/gamma/theta/vega. Safe for edge cases."""
    out = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    try:
        if not SCIPY_AVAILABLE or spot <= 0 or strike <= 0 or t_years <= 0 or iv <= 0:
            return out
        d1 = (math.log(spot / strike) + (r + 0.5 * iv ** 2) * t_years) / (iv * math.sqrt(t_years))
        d2 = d1 - iv * math.sqrt(t_years)
        pdf_d1 = norm.pdf(d1)
        if opt_type == "CE":
            delta = norm.cdf(d1)
            theta = (-(spot * pdf_d1 * iv) / (2 * math.sqrt(t_years))
                     - r * strike * math.exp(-r * t_years) * norm.cdf(d2)) / 365
        else:
            delta = norm.cdf(d1) - 1
            theta = (-(spot * pdf_d1 * iv) / (2 * math.sqrt(t_years))
                     + r * strike * math.exp(-r * t_years) * norm.cdf(-d2)) / 365
        gamma = pdf_d1 / (spot * iv * math.sqrt(t_years))
        vega = spot * pdf_d1 * math.sqrt(t_years) / 100
        out.update(delta=safe_round(delta, 4), gamma=safe_round(gamma, 5),
                    theta=safe_round(theta, 3), vega=safe_round(vega, 3))
    except Exception:
        pass
    return out


def analyze_option_chain(chain_json: Optional[dict], spot: float) -> dict:
    """
    Parses the NSE option-chain JSON into the analytics this engine needs.
    Returns a fully-populated, NaN-safe dict even if chain_json is None.
    """
    result = {
        "available": False,
        "pcr": float("nan"),
        "ce_oi_total": float("nan"),
        "pe_oi_total": float("nan"),
        "oi_change_ce": float("nan"),
        "oi_change_pe": float("nan"),
        "avg_iv": float("nan"),
        "max_pain": float("nan"),
        "atm_strike": float("nan"),
        "itm_strike": float("nan"),
        "otm_strike": float("nan"),
        "best_expiry": "N/A",
        "ce_ltp_atm": float("nan"),
        "pe_ltp_atm": float("nan"),
        "writers_bias": "Unknown",
    }
    if not chain_json:
        return result
    try:
        records = chain_json.get("records", {})
        expiries = records.get("expiryDates", [])
        if not expiries:
            return result
        best_expiry = expiries[0]  # nearest expiry = most liquid, default choice
        result["best_expiry"] = best_expiry

        data = [d for d in records.get("data", []) if d.get("expiryDate") == best_expiry]
        if not data:
            return result

        strikes = sorted(set(d["strikePrice"] for d in data))
        atm_strike = min(strikes, key=lambda s: abs(s - spot)) if strikes else float("nan")
        step = min([abs(strikes[i + 1] - strikes[i]) for i in range(len(strikes) - 1)], default=50) if len(strikes) > 1 else 50
        itm_strike = atm_strike - step if not math.isnan(atm_strike) else float("nan")
        otm_strike = atm_strike + step if not math.isnan(atm_strike) else float("nan")

        ce_oi_total = pe_oi_total = ce_oi_chg = pe_oi_chg = 0.0
        iv_values = []
        pain_by_strike = {}

        for row in data:
            ce, pe = row.get("CE"), row.get("PE")
            strike = row.get("strikePrice", 0)
            if ce:
                ce_oi_total += safe_float(ce.get("openInterest"))
                ce_oi_chg += safe_float(ce.get("changeinOpenInterest"))
                if safe_float(ce.get("impliedVolatility")) > 0:
                    iv_values.append(safe_float(ce.get("impliedVolatility")))
            if pe:
                pe_oi_total += safe_float(pe.get("openInterest"))
                pe_oi_chg += safe_float(pe.get("changeinOpenInterest"))
                if safe_float(pe.get("impliedVolatility")) > 0:
                    iv_values.append(safe_float(pe.get("impliedVolatility")))

        # Max Pain: strike at which total option-writer payout is minimized
        for target_row in data:
            target_strike = target_row.get("strikePrice", 0)
            total_loss = 0.0
            for row in data:
                strike = row.get("strikePrice", 0)
                ce, pe = row.get("CE"), row.get("PE")
                if ce:
                    ce_oi = safe_float(ce.get("openInterest"))
                    total_loss += max(target_strike - strike, 0) * ce_oi
                if pe:
                    pe_oi = safe_float(pe.get("openInterest"))
                    total_loss += max(strike - target_strike, 0) * pe_oi
            pain_by_strike[target_strike] = total_loss
        max_pain = min(pain_by_strike, key=pain_by_strike.get) if pain_by_strike else float("nan")

        atm_row = next((r for r in data if r.get("strikePrice") == atm_strike), None)
        ce_ltp_atm = safe_float(atm_row.get("CE", {}).get("lastPrice")) if atm_row and atm_row.get("CE") else float("nan")
        pe_ltp_atm = safe_float(atm_row.get("PE", {}).get("lastPrice")) if atm_row and atm_row.get("PE") else float("nan")

        pcr = safe_float(pe_oi_total) / ce_oi_total if ce_oi_total else float("nan")

        # Writers' bias heuristic: strong CE OI build-up with muted price
        # rise = call writers defending resistance (bearish bias) and vice
        # versa for PE OI build-up (bullish bias).
        if ce_oi_chg > pe_oi_chg * 1.2:
            writers_bias = "Bearish (Call writers active)"
        elif pe_oi_chg > ce_oi_chg * 1.2:
            writers_bias = "Bullish (Put writers active)"
        else:
            writers_bias = "Neutral"

        result.update(
            available=True,
            pcr=safe_round(pcr, 3),
            ce_oi_total=ce_oi_total,
            pe_oi_total=pe_oi_total,
            oi_change_ce=ce_oi_chg,
            oi_change_pe=pe_oi_chg,
            avg_iv=safe_round(np.mean(iv_values), 2) if iv_values else float("nan"),
            max_pain=safe_float(max_pain),
            atm_strike=safe_float(atm_strike),
            itm_strike=safe_float(itm_strike),
            otm_strike=safe_float(otm_strike),
            ce_ltp_atm=ce_ltp_atm,
            pe_ltp_atm=pe_ltp_atm,
            writers_bias=writers_bias,
        )
    except Exception:
        pass
    return result


def days_to_expiry(expiry_str: str) -> float:
    try:
        exp_date = dt.datetime.strptime(expiry_str, "%d-%b-%Y").date()
        delta = (exp_date - dt.date.today()).days
        return max(delta, 1)
    except Exception:
        return 7.0


# ============================================================================
# SECTION 7 : SCORING / ENSEMBLE MODEL  (rule-based + XGBoost)
# ============================================================================

def build_feature_row(df: pd.DataFrame) -> dict:
    """All raw technical features for one symbol's latest bar."""
    close = df["Close"]
    feat = {}
    feat["ema20"] = safe_float(ema(close, 20).iloc[-1])
    feat["ema50"] = safe_float(ema(close, 50).iloc[-1])
    feat["ema200"] = safe_float(ema(close, 200).iloc[-1]) if len(df) >= 200 else safe_float(ema(close, min(len(df) - 1, 100)).iloc[-1])
    feat["rsi"] = safe_float(rsi(close).iloc[-1], 50)
    macd_line, signal_line, hist = macd(close)
    feat["macd"] = safe_float(macd_line.iloc[-1])
    feat["macd_signal"] = safe_float(signal_line.iloc[-1])
    feat["macd_hist"] = safe_float(hist.iloc[-1])
    feat["atr"] = safe_float(atr(df).iloc[-1])
    st_line, st_dir = supertrend(df)
    feat["supertrend_dir"] = int(st_dir.iloc[-1]) if not pd.isna(st_dir.iloc[-1]) else 0
    feat["supertrend_val"] = safe_float(st_line.iloc[-1])
    feat["adx"] = safe_float(adx(df).iloc[-1])
    bb_u, bb_m, bb_l = bollinger_bands(close)
    feat["bb_upper"] = safe_float(bb_u.iloc[-1])
    feat["bb_lower"] = safe_float(bb_l.iloc[-1])
    feat["bb_mid"] = safe_float(bb_m.iloc[-1])
    feat["vwap"] = safe_float(vwap(df).iloc[-1])
    feat["rel_vol"] = relative_volume(df)
    feat["close"] = safe_float(close.iloc[-1])
    feat["prev_close"] = safe_float(close.iloc[-2]) if len(df) > 1 else feat["close"]
    feat["day_change_pct"] = pct(feat["close"] - feat["prev_close"], feat["prev_close"])
    return feat


def momentum_score(feat: dict) -> float:
    score = 50.0
    score += clip((feat["rsi"] - 50) * 0.8, -25, 25)
    score += 15 if feat["macd_hist"] > 0 else -15
    score += 10 if feat["close"] > feat["ema20"] > feat["ema50"] else (-10 if feat["close"] < feat["ema20"] < feat["ema50"] else 0)
    return clip(score, 0, 100)


def volume_score(feat: dict) -> float:
    rv = feat["rel_vol"]
    if rv >= 2.5:
        base = 95
    elif rv >= 1.8:
        base = 85
    elif rv >= 1.3:
        base = 72
    elif rv >= 0.9:
        base = 55
    else:
        base = 35
    return clip(base, 0, 100)


def delivery_score(delivery_pct: float) -> float:
    if math.isnan(delivery_pct):
        return 50.0
    return clip(delivery_pct * 1.1, 0, 100)


def oi_score(opt: dict, direction_hint: int) -> float:
    if not opt["available"]:
        return 50.0
    ce_chg, pe_chg = safe_float(opt["oi_change_ce"]), safe_float(opt["oi_change_pe"])
    total = abs(ce_chg) + abs(pe_chg)
    if total == 0:
        return 50.0
    if direction_hint >= 0:
        bias = pe_chg - ce_chg
    else:
        bias = ce_chg - pe_chg
    return clip(50 + (bias / total) * 50, 0, 100)


def pcr_score(pcr: float) -> float:
    if math.isnan(pcr):
        return 50.0
    if pcr >= 1.5:
        return 80
    if pcr >= 1.2:
        return 68
    if pcr >= 0.9:
        return 55
    if pcr >= 0.7:
        return 42
    return 25


def iv_score(avg_iv: float, india_vix: float) -> float:
    if math.isnan(avg_iv):
        return 50.0
    ref = india_vix if not math.isnan(india_vix) else 15.0
    diff = avg_iv - ref
    return clip(60 - diff * 1.5, 10, 90)


def smart_money_score(delivery_s: float, rel_vol: float, oi_s: float, structure: str) -> float:
    struct_bonus = 15 if structure == "Bullish" else (-15 if structure == "Bearish" else 0)
    rv_component = clip((rel_vol - 1) * 20, -20, 20)
    score = 0.4 * delivery_s + 0.3 * oi_s + 25 + rv_component * 0.3 + struct_bonus
    return clip(score, 0, 100)


def sector_index_strength(symbol_change_pct: float, index_change_pct: float) -> float:
    """Relative strength vs. index, mapped to a 0-100 score."""
    rs = symbol_change_pct - index_change_pct
    return clip(50 + rs * 8, 0, 100)


def xgb_probability(feat: dict, history_df: pd.DataFrame) -> Optional[float]:
    """
    Trains a tiny, fast XGBoost classifier per symbol on its own rolling
    history (label = next-day up/down) and returns P(up) for the latest
    bar. Falls back to None if XGBoost is unavailable or there isn't
    enough history -- caller must handle that gracefully.
    """
    if not XGB_AVAILABLE or history_df is None or len(history_df) < 120:
        return None
    try:
        df = history_df.copy()
        df["ema20"] = ema(df["Close"], 20)
        df["ema50"] = ema(df["Close"], 50)
        df["rsi"] = rsi(df["Close"])
        macd_line, signal_line, hist = macd(df["Close"])
        df["macd_hist"] = hist
        df["atr"] = atr(df)
        df["adx"] = adx(df)
        df["rel_vol"] = df["Volume"] / df["Volume"].rolling(20).mean()
        df["ret1"] = df["Close"].pct_change()
        df["target"] = (df["Close"].shift(-1) > df["Close"]).astype(int)

        feature_cols = ["ema20", "ema50", "rsi", "macd_hist", "atr", "adx", "rel_vol", "ret1"]
        model_df = df[feature_cols + ["target"]].dropna()
        if len(model_df) < 60:
            return None

        X_train = model_df[feature_cols].iloc[:-1]
        y_train = model_df["target"].iloc[:-1]
        X_last = model_df[feature_cols].iloc[[-1]]

        clf = xgb.XGBClassifier(
            n_estimators=80, max_depth=3, learning_rate=0.08,
            subsample=0.8, colsample_bytree=0.8, eval_metric="logloss",
            verbosity=0,
        )
        clf.fit(X_train, y_train)
        proba_up = clf.predict_proba(X_last)[0][1]
        return safe_float(proba_up, 0.5)
    except Exception:
        return None


# ============================================================================
# SECTION 8 : TRADE PLAN GENERATOR  (equity + options)
# ============================================================================

def lot_size_for(symbol: str, spot: float) -> int:
    if symbol in KNOWN_LOT_SIZES:
        return KNOWN_LOT_SIZES[symbol]
    if spot <= 0:
        return 1
    approx = max(int(round(DEFAULT_LOT_SIZE_NOTIONAL / spot / 25) * 25), 25)
    return approx


def option_strike_step(spot: float) -> float:
    if spot < 250:
        return 5
    if spot < 1000:
        return 10
    if spot < 3000:
        return 25
    return 50


@dataclass
class TradePlan:
    entry: float = 0.0
    stop_loss: float = 0.0
    target1: float = 0.0
    target2: float = 0.0
    target3: float = 0.0
    rr: float = 0.0
    expected_move_pct: float = 0.0
    expected_points: float = 0.0
    ce_strike: float = 0.0
    pe_strike: float = 0.0
    ce_entry: float = 0.0
    ce_sl: float = 0.0
    ce_target: float = 0.0
    pe_entry: float = 0.0
    pe_sl: float = 0.0
    pe_target: float = 0.0
    qty: int = 0
    capital: float = 0.0
    roi: float = 0.0


def build_trade_plan(spot: float, atr_val: float, direction: str, confidence: float,
                      opt: dict, iv_ref: float, symbol: str, capital: float,
                      risk_pct: float) -> TradePlan:
    plan = TradePlan()
    atr_val = atr_val if atr_val > 0 else spot * 0.015

    if direction in ("Strong Bullish", "Bullish"):
        plan.entry = spot
        plan.stop_loss = spot - 1.2 * atr_val
        plan.target1 = spot + 1.0 * atr_val
        plan.target2 = spot + 1.8 * atr_val
        plan.target3 = spot + 2.8 * atr_val
    elif direction in ("Strong Bearish", "Bearish"):
        plan.entry = spot
        plan.stop_loss = spot + 1.2 * atr_val
        plan.target1 = spot - 1.0 * atr_val
        plan.target2 = spot - 1.8 * atr_val
        plan.target3 = spot - 2.8 * atr_val
    else:
        plan.entry = spot
        plan.stop_loss = spot - 1.0 * atr_val
        plan.target1 = spot + 0.6 * atr_val
        plan.target2 = spot + 1.0 * atr_val
        plan.target3 = spot + 1.4 * atr_val

    risk = abs(plan.entry - plan.stop_loss)
    reward = abs(plan.target2 - plan.entry)
    plan.rr = safe_round(reward / risk, 2) if risk > 0 else 0.0
    plan.expected_move_pct = safe_round(pct(plan.target2 - plan.entry, plan.entry), 2)
    plan.expected_points = safe_round(plan.target2 - plan.entry, 2)

    step = option_strike_step(spot)
    atm = round(spot / step) * step
    is_bullish = direction in ("Strong Bullish", "Bullish")
    plan.ce_strike = atm if is_bullish else atm + step
    plan.pe_strike = atm if not is_bullish else atm - step

    expiry_str = opt.get("best_expiry", "N/A") if opt else "N/A"
    dte = days_to_expiry(expiry_str) if expiry_str != "N/A" else 7.0
    t_years = dte / 365.0
    iv_used = iv_ref if not math.isnan(iv_ref) and iv_ref > 0 else 22.0

    ce_ltp = opt.get("ce_ltp_atm") if opt else float("nan")
    pe_ltp = opt.get("pe_ltp_atm") if opt else float("nan")

    def bs_price(strike, opt_type):
        try:
            if not SCIPY_AVAILABLE or spot <= 0 or t_years <= 0:
                return float("nan")
            sigma = iv_used / 100.0
            d1 = (math.log(spot / strike) + (RISK_FREE_RATE + 0.5 * sigma ** 2) * t_years) / (sigma * math.sqrt(t_years))
            d2 = d1 - sigma * math.sqrt(t_years)
            if opt_type == "CE":
                price = spot * norm.cdf(d1) - strike * math.exp(-RISK_FREE_RATE * t_years) * norm.cdf(d2)
            else:
                price = strike * math.exp(-RISK_FREE_RATE * t_years) * norm.cdf(-d2) - spot * norm.cdf(-d1)
            return max(price, 0.05)
        except Exception:
            return float("nan")

    ce_theo = ce_ltp if not math.isnan(safe_float(ce_ltp, float("nan"))) else bs_price(plan.ce_strike, "CE")
    pe_theo = pe_ltp if not math.isnan(safe_float(pe_ltp, float("nan"))) else bs_price(plan.pe_strike, "PE")
    ce_theo = safe_float(ce_theo, spot * 0.02)
    pe_theo = safe_float(pe_theo, spot * 0.02)

    plan.ce_entry = safe_round(ce_theo, 2)
    plan.ce_sl = safe_round(ce_theo * 0.55, 2)
    plan.ce_target = safe_round(ce_theo * (1.7 if confidence >= 80 else 1.4), 2)

    plan.pe_entry = safe_round(pe_theo, 2)
    plan.pe_sl = safe_round(pe_theo * 0.55, 2)
    plan.pe_target = safe_round(pe_theo * (1.7 if confidence >= 80 else 1.4), 2)

    lot = lot_size_for(symbol, spot)
    risk_amount = capital * (risk_pct / 100.0)
    qty_by_risk = int(risk_amount / risk) if risk > 0 else lot
    plan.qty = max(lot, (qty_by_risk // lot) * lot) if qty_by_risk >= lot else lot
    plan.capital = safe_round(plan.qty * plan.entry, 2)
    plan.roi = safe_round(pct(plan.qty * (plan.target2 - plan.entry), plan.capital), 2) if plan.capital else 0.0

    return plan


# ============================================================================
# SECTION 9 : PER-SYMBOL SCAN PIPELINE
# ============================================================================

def classify_direction(confidence_bull: float) -> str:
    if confidence_bull >= 90:
        return "Strong Bullish"
    if confidence_bull >= 80:
        return "Bullish"
    if confidence_bull >= 60:
        return "Neutral"
    if confidence_bull >= 40:
        return "Bearish"
    return "Strong Bearish"


def trend_label(structure: str, feat: dict) -> str:
    if structure in ("Bullish", "Bearish"):
        return structure
    if feat["adx"] < 18:
        return "Sideways"
    return "Bullish" if feat["close"] > feat["ema50"] else "Bearish"


def scan_symbol(symbol: str, india_vix: float, index_change_pct: float, capital: float, risk_pct: float) -> Optional[dict]:
    df = fetch_price_history(symbol)
    if df is None or len(df) < 30:
        return None

    try:
        feat = build_feature_row(df)
        spot = feat["close"]
        if spot <= 0:
            return None

        structure_info = market_structure_analysis(df)
        structure = structure_info["structure"]
        trend = trend_label(structure, feat)

        delivery_pct = fetch_delivery_pct(symbol)
        chain_json = fetch_option_chain(symbol)
        opt = analyze_option_chain(chain_json, spot)
        news_sent = fetch_news_sentiment(symbol)

        mom_s = momentum_score(feat)
        vol_s = volume_score(feat)
        del_s = delivery_score(delivery_pct)
        direction_hint = 1 if feat["close"] > feat["ema50"] else -1
        oi_s = oi_score(opt, direction_hint)
        pcr_s = pcr_score(opt["pcr"])
        iv_s = iv_score(opt["avg_iv"], india_vix)
        smc_s = smart_money_score(del_s, feat["rel_vol"], oi_s, structure)
        sector_s = sector_index_strength(feat["day_change_pct"], index_change_pct)

        ml_proba = xgb_probability(feat, df)

        # Rule-based bullish probability (0-100) blending every signal
        rule_components = {
            "momentum": (mom_s, 0.16),
            "volume": (vol_s, 0.10),
            "delivery": (del_s, 0.08),
            "oi": (oi_s, 0.12),
            "pcr": (pcr_s, 0.08),
            "iv": (iv_s, 0.05),
            "smart_money": (smc_s, 0.14),
            "sector": (sector_s, 0.09),
            "adx_trend": (clip(50 + (feat["adx"] - 20) * (1 if direction_hint > 0 else -1), 0, 100), 0.08),
            "supertrend": (70 if feat["supertrend_dir"] == 1 else 30, 0.06),
            "news": (clip(50 + news_sent * 30, 0, 100), 0.04),
        }
        rule_bull_pct = sum(v * w for v, w in rule_components.values())

        if ml_proba is not None:
            ml_pct = ml_proba * 100
            final_bull_pct = 0.55 * rule_bull_pct + 0.45 * ml_pct
            model_used = "SMC+TA Ensemble + XGBoost"
        else:
            final_bull_pct = rule_bull_pct
            model_used = "SMC+TA Ensemble (rule-based)"

        final_bull_pct = clip(final_bull_pct, 1, 99)
        direction_label = classify_direction(final_bull_pct)
        is_bullish_side = final_bull_pct >= 50
        confidence = final_bull_pct if is_bullish_side else 100 - final_bull_pct

        institutional_score = clip(
            0.35 * smc_s + 0.25 * oi_s + 0.20 * del_s + 0.20 * vol_s, 0, 100
        )

        breakout_prob = clip(confidence * 0.9 + (feat["adx"] - 20) * 0.5, 0, 100) if is_bullish_side else clip(30 - (confidence - 50), 0, 100)
        breakdown_prob = clip(confidence * 0.9 + (feat["adx"] - 20) * 0.5, 0, 100) if not is_bullish_side else clip(30 - (confidence - 50), 0, 100)
        swing_prob = clip(0.5 * confidence + 0.5 * institutional_score, 0, 100)
        intraday_prob = clip(0.6 * vol_s + 0.4 * mom_s, 0, 100)
        position_trade_prob = clip(0.5 * institutional_score + 0.5 * (100 - abs(feat["adx"] - 25)), 0, 100)

        option_buying_score = clip(confidence * 0.6 + (100 - iv_s) * 0.4, 0, 100)
        option_selling_score = clip((100 - confidence) * 0.3 + iv_s * 0.7, 0, 100) if not math.isnan(opt["avg_iv"]) else 50.0

        atr_val = feat["atr"]
        plan = build_trade_plan(spot, atr_val, direction_label, confidence, opt,
                                 opt["avg_iv"], symbol, capital, risk_pct)

        max_pain_distance = safe_round(pct(spot - opt["max_pain"], opt["max_pain"])) if not math.isnan(opt["max_pain"]) and opt["max_pain"] else float("nan")

        buy_sell_strength = safe_round(confidence if is_bullish_side else -confidence, 1)

        remarks_bits = [structure]
        if structure_info["bos"] != "None":
            remarks_bits.append(structure_info["bos"])
        if structure_info["choch"] != "None":
            remarks_bits.append(structure_info["choch"])
        if structure_info["liquidity_sweep"] != "None":
            remarks_bits.append(structure_info["liquidity_sweep"])
        if structure_info["fvg"] != "None":
            remarks_bits.append(structure_info["fvg"])
        remarks_bits.append(f"RSI {safe_round(feat['rsi'],1)}")
        remarks_bits.append(f"RelVol {safe_round(feat['rel_vol'],2)}x")
        if not opt["available"]:
            remarks_bits.append("Option chain unavailable - equity signals only")
        remarks_bits.append(model_used)
        ai_remarks = " | ".join(remarks_bits)

        row = {
            "Symbol": symbol,
            "Spot Price": safe_round(spot, 2),
            "AI Direction": "BUY" if is_bullish_side else "SELL",
            "AI Confidence %": safe_round(confidence, 2),
            "Institutional Score": safe_round(institutional_score, 2),
            "Trend": trend,
            "Direction Class": direction_label,
            "Entry Price": safe_round(plan.entry, 2),
            "Stop Loss": safe_round(plan.stop_loss, 2),
            "Target 1": safe_round(plan.target1, 2),
            "Target 2": safe_round(plan.target2, 2),
            "Target 3": safe_round(plan.target3, 2),
            "Risk Reward Ratio": plan.rr,
            "Expected Move %": plan.expected_move_pct,
            "Expected Points": plan.expected_points,
            "Buy/Sell Strength": buy_sell_strength,
            "Momentum Score": safe_round(mom_s, 1),
            "Volume Score": safe_round(vol_s, 1),
            "Delivery Score": safe_round(del_s, 1),
            "OI Score": safe_round(oi_s, 1),
            "PCR Score": safe_round(pcr_s, 1),
            "IV Score": safe_round(iv_s, 1),
            "Smart Money Score": safe_round(smc_s, 1),
            "Breakout Probability": safe_round(breakout_prob, 1),
            "Breakdown Probability": safe_round(breakdown_prob, 1),
            "Swing Probability": safe_round(swing_prob, 1),
            "Intraday Probability": safe_round(intraday_prob, 1),
            "Position Trade Probability": safe_round(position_trade_prob, 1),
            "Option Buying Score": safe_round(option_buying_score, 1),
            "Option Selling Score": safe_round(option_selling_score, 1),
            "Option Writers Bias": opt["writers_bias"],
            "Max Pain Distance": max_pain_distance if not (isinstance(max_pain_distance, float) and math.isnan(max_pain_distance)) else "N/A",
            "ATM Strike": opt["atm_strike"] if not math.isnan(opt["atm_strike"]) else "N/A",
            "ITM Strike": opt["itm_strike"] if not math.isnan(opt["itm_strike"]) else "N/A",
            "OTM Strike": opt["otm_strike"] if not math.isnan(opt["otm_strike"]) else "N/A",
            "Recommended CE Strike": plan.ce_strike,
            "Recommended PE Strike": plan.pe_strike,
            "CE Entry": plan.ce_entry,
            "CE SL": plan.ce_sl,
            "CE Target": plan.ce_target,
            "PE Entry": plan.pe_entry,
            "PE SL": plan.pe_sl,
            "PE Target": plan.pe_target,
            "Best Expiry": opt["best_expiry"],
            "Suggested Quantity": plan.qty,
            "Suggested Capital": plan.capital,
            "Expected ROI": plan.roi,
            "Expected Accuracy": safe_round(min(confidence, 92.0), 1),
            "AI Remarks": ai_remarks,
            "_rel_vol_raw": feat["rel_vol"],
            "_oi_conf_raw": oi_s,
        }
        return row
    except Exception as exc:
        return {
            "Symbol": symbol, "Spot Price": float("nan"), "AI Direction": "N/A",
            "AI Confidence %": 0.0, "Institutional Score": 0.0, "Trend": "N/A",
            "Direction Class": "N/A", "Entry Price": float("nan"), "Stop Loss": float("nan"),
            "Target 1": float("nan"), "Target 2": float("nan"), "Target 3": float("nan"),
            "Risk Reward Ratio": 0.0, "Expected Move %": 0.0, "Expected Points": 0.0,
            "Buy/Sell Strength": 0.0, "Momentum Score": 0.0, "Volume Score": 0.0,
            "Delivery Score": 0.0, "OI Score": 0.0, "PCR Score": 0.0, "IV Score": 0.0,
            "Smart Money Score": 0.0, "Breakout Probability": 0.0, "Breakdown Probability": 0.0,
            "Swing Probability": 0.0, "Intraday Probability": 0.0, "Position Trade Probability": 0.0,
            "Option Buying Score": 0.0, "Option Selling Score": 0.0, "Option Writers Bias": "Unknown",
            "Max Pain Distance": "N/A", "ATM Strike": "N/A", "ITM Strike": "N/A", "OTM Strike": "N/A",
            "Recommended CE Strike": "N/A", "Recommended PE Strike": "N/A", "CE Entry": "N/A",
            "CE SL": "N/A", "CE Target": "N/A", "PE Entry": "N/A", "PE SL": "N/A", "PE Target": "N/A",
            "Best Expiry": "N/A", "Suggested Quantity": 0, "Suggested Capital": 0.0,
            "Expected ROI": 0.0, "Expected Accuracy": 0.0,
            "AI Remarks": f"Data error for this symbol ({type(exc).__name__}) - skipped scoring",
            "_rel_vol_raw": 1.0, "_oi_conf_raw": 50.0,
        }


# ============================================================================
# SECTION 10 : FULL MARKET SCAN
# ============================================================================

def run_full_scan(symbols: list, capital: float, risk_pct: float, progress_cb=None) -> pd.DataFrame:
    india_vix = fetch_india_vix()
    index_df = fetch_index_series("^NSEI")
    index_change_pct = 0.0
    if index_df is not None and len(index_df) > 1:
        index_change_pct = pct(index_df["Close"].iloc[-1] - index_df["Close"].iloc[-2], index_df["Close"].iloc[-2])

    rows = []
    seen = set()
    total = len(symbols)
    for i, sym in enumerate(symbols):
        if sym in seen:
            continue
        seen.add(sym)
        row = scan_symbol(sym, india_vix, index_change_pct, capital, risk_pct)
        if row is not None:
            rows.append(row)
        if progress_cb:
            progress_cb((i + 1) / total, sym)

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows).drop_duplicates(subset=["Symbol"], keep="first")
    df = df.fillna("N/A")
    return df


def rank_and_split(df: pd.DataFrame):
    if df.empty:
        return pd.DataFrame(), pd.DataFrame()

    work = df.copy()
    for col in ["AI Confidence %", "Institutional Score", "_rel_vol_raw", "_oi_conf_raw"]:
        work[col] = pd.to_numeric(work[col], errors="coerce").fillna(0)

    buys = work[work["AI Direction"] == "BUY"].sort_values(
        by=["AI Confidence %", "Institutional Score", "_rel_vol_raw", "_oi_conf_raw"],
        ascending=[False, False, False, False],
    ).head(10).reset_index(drop=True)

    sells = work[work["AI Direction"] == "SELL"].sort_values(
        by=["AI Confidence %", "Institutional Score", "_rel_vol_raw", "_oi_conf_raw"],
        ascending=[False, False, False, False],
    ).head(10).reset_index(drop=True)

    for tbl in (buys, sells):
        tbl.insert(0, "Rank", range(1, len(tbl) + 1))
        tbl.drop(columns=["_rel_vol_raw", "_oi_conf_raw"], inplace=True, errors="ignore")

    return buys, sells


# ============================================================================
# SECTION 11 : STREAMLIT UI
# ============================================================================

def to_excel_bytes(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    try:
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="Signals")
    except Exception:
        with pd.ExcelWriter(buf, engine="xlsxwriter") as writer:
            df.to_excel(writer, index=False, sheet_name="Signals")
    return buf.getvalue()


def style_table(df: pd.DataFrame, side: str):
    if df.empty:
        return df
    accent = "#0ecb81" if side == "BUY" else "#f6465d"

    def highlight_conf(val):
        try:
            v = float(val)
        except Exception:
            return ""
        if v >= 90:
            return "background-color:#0ecb81;color:white;font-weight:600"
        if v >= 80:
            return "background-color:#7cd992;color:black"
        if v >= 60:
            return "background-color:#fff3b0;color:black"
        if v >= 40:
            return "background-color:#ffc48a;color:black"
        return "background-color:#f6465d;color:white"

    def highlight_dir(val):
        if val == "BUY":
            return "color:#0ecb81;font-weight:700"
        if val == "SELL":
            return "color:#f6465d;font-weight:700"
        return ""

    styled = (
        df.style
        .applymap(highlight_conf, subset=["AI Confidence %"])
        .applymap(highlight_dir, subset=["AI Direction"])
        .set_properties(**{"font-size": "13px"})
        .set_table_styles([{"selector": "th", "props": [("background-color", accent), ("color", "white")]}])
        .format(precision=2)
    )
    return styled


def render_table_block(title: str, df: pd.DataFrame, side: str, key_prefix: str):
    st.subheader(title)
    if df.empty:
        st.info("No qualifying signals found in the current scan for this side.")
        return

    search = st.text_input(f"🔍 Search {side} table (symbol / remarks)", key=f"{key_prefix}_search")
    view_df = df.copy()
    if search:
        mask = view_df.apply(lambda r: search.lower() in str(r).lower(), axis=1)
        view_df = view_df[mask]

    sort_col = st.selectbox(
        f"Sort {side} table by", options=list(view_df.columns),
        index=list(view_df.columns).index("AI Confidence %") if "AI Confidence %" in view_df.columns else 0,
        key=f"{key_prefix}_sort_col",
    )
    sort_asc = st.checkbox(f"Ascending order ({side})", value=False, key=f"{key_prefix}_sort_asc")
    try:
        view_df = view_df.sort_values(by=sort_col, ascending=sort_asc, key=lambda s: pd.to_numeric(s, errors="ignore"))
    except Exception:
        view_df = view_df.sort_values(by=sort_col, ascending=sort_asc)

    st.dataframe(style_table(view_df, side), use_container_width=True, height=420)

    col1, col2 = st.columns(2)
    with col1:
        st.download_button(
            f"⬇️ Export {side} CSV", data=view_df.to_csv(index=False).encode("utf-8"),
            file_name=f"fno_{side.lower()}_signals_{dt.date.today()}.csv",
            mime="text/csv", key=f"{key_prefix}_csv",
        )
    with col2:
        st.download_button(
            f"⬇️ Export {side} Excel", data=to_excel_bytes(view_df),
            file_name=f"fno_{side.lower()}_signals_{dt.date.today()}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=f"{key_prefix}_xlsx",
        )


def main():
    st.set_page_config(page_title=APP_TITLE, layout="wide", page_icon="📊")

    st.markdown(
        """
        <style>
        .main-header {font-size:2rem;font-weight:800;margin-bottom:0;}
        .sub-header {color:#9aa0a6;margin-top:0;margin-bottom:1.2rem;}
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.markdown(f'<p class="main-header">📊 {APP_TITLE}</p>', unsafe_allow_html=True)
    st.markdown(
        '<p class="sub-header">Institutional-style multi-factor F&O ranking engine '
        '&mdash; SMC + Technicals + Option Chain + XGBoost Ensemble</p>',
        unsafe_allow_html=True,
    )

    with st.expander("⚠️ Data & methodology disclaimer (please read)"):
        st.write(
            "All scores are computed from publicly available price, volume, delivery-proxy "
            "and NSE option-chain data using standard quant/retail proxies for institutional "
            "activity. This is a research and decision-support tool, not certified real-time "
            "institutional order-flow data, and not investment advice. 'Expected ROI' and "
            "'Expected Accuracy' are model estimates based on current signal confluence, not "
            "guarantees. Please size positions responsibly and consult a licensed advisor for "
            "investment decisions."
        )

    with st.sidebar:
        st.header("⚙️ Engine Settings")
        capital = st.number_input("Capital (₹)", min_value=10000.0, value=DEFAULT_CAPITAL, step=10000.0)
        risk_pct = st.slider("Risk per trade (%)", 0.25, 5.0, DEFAULT_RISK_PCT, 0.25)
        universe_choice = st.radio("Scan universe", ["Full F&O list", "Custom subset"], index=0)
        if universe_choice == "Custom subset":
            symbols = st.multiselect("Choose symbols", FNO_SYMBOLS, default=FNO_SYMBOLS[:15])
        else:
            symbols = FNO_SYMBOLS

        max_symbols = st.slider("Max symbols to scan this run (perf control)", 10, len(FNO_SYMBOLS), min(60, len(FNO_SYMBOLS)))
        symbols = symbols[:max_symbols]

        st.divider()
        auto_refresh_on = st.checkbox("🔁 Auto-refresh scan", value=False)
        refresh_secs = st.number_input("Refresh interval (seconds)", min_value=30, value=180, step=30, disabled=not auto_refresh_on)

        st.divider()
        st.caption(f"yfinance: {'✅' if YF_AVAILABLE else '❌ not installed'}")
        st.caption(f"scipy (Greeks): {'✅' if SCIPY_AVAILABLE else '❌ not installed'}")
        st.caption(f"xgboost: {'✅' if XGB_AVAILABLE else '❌ not installed (rule-based fallback used)'}")

        run_scan = st.button("🚀 Run Full Scan", type="primary", use_container_width=True)

    if auto_refresh_on and AUTOREFRESH_AVAILABLE:
        st_autorefresh(interval=int(refresh_secs * 1000), key="fno_autorefresh")
    elif auto_refresh_on and not AUTOREFRESH_AVAILABLE:
        st.sidebar.warning("Install `streamlit-autorefresh` to enable true auto-refresh.")

    if "scan_df" not in st.session_state:
        st.session_state["scan_df"] = pd.DataFrame()
        st.session_state["last_scan_time"] = None

    should_scan = run_scan or (auto_refresh_on and AUTOREFRESH_AVAILABLE)

    if should_scan:
        progress_bar = st.progress(0.0, text="Starting scan...")
        status_text = st.empty()

        def _cb(fraction, sym):
            progress_bar.progress(fraction, text=f"Scanning {sym} ({int(fraction*100)}%)")

        with st.spinner("Running institutional multi-factor scan across NSE F&O universe..."):
            scan_df = run_full_scan(symbols, capital, risk_pct, progress_cb=_cb)

        progress_bar.empty()
        status_text.empty()
        st.session_state["scan_df"] = scan_df
        st.session_state["last_scan_time"] = dt.datetime.now()

    scan_df = st.session_state["scan_df"]
    last_time = st.session_state["last_scan_time"]

    if scan_df is None or scan_df.empty:
        st.info("👈 Configure settings in the sidebar and click **Run Full Scan** to generate signals.")
        return

    if last_time:
        st.caption(f"Last scan: {last_time.strftime('%Y-%m-%d %H:%M:%S')} | "
                    f"Symbols scanned: {len(scan_df)} | Unique symbols: {scan_df['Symbol'].nunique()}")

    buys, sells = rank_and_split(scan_df)

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Total Scanned", len(scan_df))
    m2.metric("BUY Signals ≥60%", int((pd.to_numeric(scan_df["AI Confidence %"], errors="coerce") >= 60).sum()))
    m3.metric("Top BUY Confidence", f"{safe_round(buys['AI Confidence %'].max()) if not buys.empty else 0}%")
    m4.metric("Top SELL Confidence", f"{safe_round(sells['AI Confidence %'].max()) if not sells.empty else 0}%")

    st.divider()
    render_table_block("🟢 TOP 10 F&O BUY STOCKS", buys, "BUY", "buy")
    st.divider()
    render_table_block("🔴 TOP 10 F&O SELL STOCKS", sells, "SELL", "sell")

    st.divider()
    with st.expander("📄 View full raw scan (all symbols, all columns)"):
        st.dataframe(scan_df, use_container_width=True, height=500)
        st.download_button(
            "⬇️ Export Full Scan CSV", data=scan_df.to_csv(index=False).encode("utf-8"),
            file_name=f"fno_full_scan_{dt.date.today()}.csv", mime="text/csv",
        )


if __name__ == "__main__":
    main()
