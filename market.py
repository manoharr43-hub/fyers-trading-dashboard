"""
AI MARKET INTELLIGENCE MODULE
================================
Additive module for the FYERS Streamlit Stock Scanner.

INTEGRATION (does NOT touch your existing scanner code):
----------------------------------------------------------
In your main app file, add:

    from ai_market_intelligence import show_ai_market_intelligence

Then, wherever you render your existing pages/tabs, add a new tab/page call:

    show_ai_market_intelligence(fyers)

That's it. Nothing in your existing scanner, Order Block / FVG / BOS / CHOCH /
CISD logic, or Buy/Sell signal code is imported, modified, or executed by
this file.

HONEST NOTES (please read):
----------------------------------------------------------
1. FII/DII: NSE has no stable public JSON API for this. This module tries a
   best-effort fetch and automatically falls back to a manual-entry card if
   NSE blocks/changes the endpoint (common on cloud IPs). This is normal —
   not a bug in this code.
2. News sentiment: uses a transparent finance keyword-lexicon classifier
   (fast, free, no external key needed) — not a trained LLM. You can later
   swap `classify_sentiment()` for a real LLM call if you want higher
   accuracy; the interface is a single function so it's a one-line swap.
3. Smart Money Concepts (Order Block / FVG / BOS / CHOCH) here are real,
   deterministic swing-structure detectors — separate copies, so they will
   NOT interfere with whatever Order Block/FVG code already exists in your
   scanner.
4. The final AI signal (STRONG BUY...STRONG SELL) is a transparent weighted
   composite of the signals below, not a trained ML model. It is explainable
   by design so you can see exactly why it fired.

Dependencies to add to requirements.txt:
    requests
    feedparser
    streamlit-autorefresh
(pandas / numpy / streamlit are assumed already present in your app.)
"""

import time
import re
import math
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import streamlit as st

try:
    import requests
except ImportError:
    requests = None

try:
    import feedparser
except ImportError:
    feedparser = None

try:
    from streamlit_autorefresh import st_autorefresh
    _HAS_AUTOREFRESH = True
except ImportError:
    _HAS_AUTOREFRESH = False


# =============================================================================
# CONSTANTS
# =============================================================================

INDEX_SYMBOLS = {
    "NIFTY50": "NSE:NIFTY50-INDEX",
    "BANKNIFTY": "NSE:NIFTYBANK-INDEX",
    "FINNIFTY": "NSE:FINNIFTY-INDEX",
    "MIDCPNIFTY": "NSE:MIDCPNIFTY-INDEX",
    "SENSEX": "BSE:SENSEX-INDEX",
    "VIX": "NSE:INDIAVIX-INDEX",
}

FUTURES_HINT = ["Nifty Futures", "BankNifty Futures", "FinNifty Futures"]

NEWS_FEEDS = [
    ("Economy/Markets", "https://www.moneycontrol.com/rss/marketreports.xml"),
    ("Business", "https://www.moneycontrol.com/rss/business.xml"),
    ("Results", "https://www.moneycontrol.com/rss/results.xml"),
    ("Economy", "https://www.moneycontrol.com/rss/economy.xml"),
]

BULLISH_WORDS = [
    "surge", "rally", "jump", "gain", "record high", "beats estimates",
    "upgrade", "buyback", "strong growth", "outperform", "bull", "rebound",
    "rate cut", "inflows", "profit rises", "expands", "wins order",
    "raises guidance", "all-time high", "breakout", "positive", "upbeat",
]
BEARISH_WORDS = [
    "slump", "crash", "plunge", "fall", "miss estimates", "downgrade",
    "sell-off", "selloff", "weak", "bear", "recession fears", "rate hike",
    "outflows", "profit falls", "contracts", "loses order", "cuts guidance",
    "all-time low", "breakdown", "negative", "concerns", "default", "fraud",
]

CATEGORY_KEYWORDS = {
    "RBI": ["rbi", "repo rate", "monetary policy", "reserve bank"],
    "IPO": ["ipo", "listing", "subscription", "grey market"],
    "Results": ["q1 results", "q2 results", "q3 results", "q4 results",
                "quarterly results", "net profit", "earnings"],
    "Crude Oil": ["crude", "opec", "brent", "wti"],
    "Gold": ["gold price", "gold rate", "bullion"],
    "US Market": ["dow jones", "nasdaq", "s&p 500", "fed", "us market", "wall street"],
    "SGX Gift Nifty": ["gift nifty", "sgx nifty"],
    "Global": ["china", "europe", "global market", "asian market", "crude oil"],
    "Company": ["ltd", "limited", "shares of", "stock of"],
    "Economy": ["gdp", "inflation", "cpi", "wpi", "fiscal deficit", "economy"],
}


# =============================================================================
# SECTION 1: FII / DII DATA
# =============================================================================

@st.cache_data(ttl=300, show_spinner=False)
def fetch_fii_dii_data():
    """
    Best-effort fetch of FII/DII cash market activity.
    Returns dict with keys: fii_buy, fii_sell, dii_buy, dii_sell, date, source
    Falls back to None if unavailable (caller must handle manual entry UI).
    """
    if requests is None:
        return None

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
        "Accept": "application/json",
    }
    try:
        session = requests.Session()
        # Warm-up request required by NSE to set cookies
        session.get("https://www.nseindia.com", headers=headers, timeout=5)
        url = "https://www.nseindia.com/api/fiidiiTradeReact"
        resp = session.get(url, headers=headers, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            # data is a list with FII and DII rows
            fii_row = next((r for r in data if r.get("category", "").upper().startswith("FII")), None)
            dii_row = next((r for r in data if r.get("category", "").upper().startswith("DII")), None)
            if fii_row and dii_row:
                return {
                    "date": fii_row.get("date", datetime.now().strftime("%d-%b-%Y")),
                    "fii_buy": float(fii_row.get("buyValue", 0)),
                    "fii_sell": float(fii_row.get("sellValue", 0)),
                    "dii_buy": float(dii_row.get("buyValue", 0)),
                    "dii_sell": float(dii_row.get("sellValue", 0)),
                    "source": "NSE (live)",
                }
    except Exception:
        pass
    return None


def render_fii_dii_card():
    st.subheader("💰 FII / DII Activity")

    data = fetch_fii_dii_data()

    if data is None:
        st.warning(
            "Live FII/DII feed unavailable right now (NSE frequently blocks "
            "cloud-hosted requests). Enter today's figures manually — the "
            "dashboard will use these for sentiment scoring until the next "
            "successful auto-fetch."
        )
        with st.expander("Manual FII/DII entry", expanded=True):
            c1, c2 = st.columns(2)
            with c1:
                fii_buy = st.number_input("FII Buy (₹ Cr)", value=0.0, key="man_fii_buy")
                dii_buy = st.number_input("DII Buy (₹ Cr)", value=0.0, key="man_dii_buy")
            with c2:
                fii_sell = st.number_input("FII Sell (₹ Cr)", value=0.0, key="man_fii_sell")
                dii_sell = st.number_input("DII Sell (₹ Cr)", value=0.0, key="man_dii_sell")
            data = {
                "date": datetime.now().strftime("%d-%b-%Y"),
                "fii_buy": fii_buy, "fii_sell": fii_sell,
                "dii_buy": dii_buy, "dii_sell": dii_sell,
                "source": "Manual entry",
            }

    net_fii = data["fii_buy"] - data["fii_sell"]
    net_dii = data["dii_buy"] - data["dii_sell"]

    def clr(v):
        return "green" if v >= 0 else "red"

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("FII Buy", f"₹{data['fii_buy']:,.0f} Cr")
    c2.metric("FII Sell", f"₹{data['fii_sell']:,.0f} Cr")
    c3.metric("DII Buy", f"₹{data['dii_buy']:,.0f} Cr")
    c4.metric("DII Sell", f"₹{data['dii_sell']:,.0f} Cr")

    c5, c6 = st.columns(2)
    c5.markdown(f"**Net FII:** :{clr(net_fii)}[₹{net_fii:,.0f} Cr]")
    c6.markdown(f"**Net DII:** :{clr(net_dii)}[₹{net_dii:,.0f} Cr]")

    sentiment = compute_market_sentiment(net_fii, net_dii)
    st.markdown(f"### Market Sentiment: **{sentiment}**")
    st.caption(f"Source: {data['source']} | As of {data['date']}")

    return {"net_fii": net_fii, "net_dii": net_dii, "sentiment": sentiment}


def compute_market_sentiment(net_fii, net_dii):
    combined = net_fii + net_dii
    # thresholds in ₹ Cr, tune as needed
    if combined > 5000:
        return "Very Bullish"
    elif combined > 1000:
        return "Bullish"
    elif combined > -1000:
        return "Neutral"
    elif combined > -5000:
        return "Bearish"
    else:
        return "Very Bearish"


# =============================================================================
# SECTION 2 & 3: LIVE NEWS + AI NEWS ANALYSIS
# =============================================================================

def classify_sentiment(headline: str):
    """Lightweight lexicon-based sentiment classifier with confidence score."""
    text = headline.lower()
    bull_hits = sum(1 for w in BULLISH_WORDS if w in text)
    bear_hits = sum(1 for w in BEARISH_WORDS if w in text)

    if bull_hits == 0 and bear_hits == 0:
        return "Neutral", 60
    if bull_hits > bear_hits:
        conf = min(90, 70 + (bull_hits - bear_hits) * 10)
        return "Bullish", conf
    elif bear_hits > bull_hits:
        conf = min(90, 70 + (bear_hits - bull_hits) * 10)
        return "Bearish", conf
    else:
        return "Neutral", 60


def classify_category(headline: str):
    text = headline.lower()
    for cat, keywords in CATEGORY_KEYWORDS.items():
        if any(k in text for k in keywords):
            return cat
    return "Economy"


def extract_impacted_symbols(headline: str, watch_list):
    """Very simple heuristic: flag stock names mentioned in the headline."""
    hits = []
    for sym in watch_list:
        name = sym.split(":")[-1].replace("-EQ", "").replace("-INDEX", "")
        if name.lower() in headline.lower():
            hits.append(name)
    return hits


@st.cache_data(ttl=120, show_spinner=False)
def fetch_live_news():
    if feedparser is None:
        return []
    items = []
    for label, url in NEWS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:8]:
                published = entry.get("published", "")
                items.append({
                    "time": published,
                    "headline": entry.get("title", "").strip(),
                    "feed_label": label,
                })
        except Exception:
            continue
    return items


def render_news_panel(watch_list=None):
    st.subheader("📰 Live Market News")

    if feedparser is None:
        st.info("Install `feedparser` (add to requirements.txt) to enable the live news feed.")
        return [], {"Nifty": [], "Bank Nifty": [], "Sensex": [], "Midcap": [], "FinNifty": []}, [], []

    raw_news = fetch_live_news()
    if not raw_news:
        st.info("No live news available right now.")
        return [], {"Nifty": [], "Bank Nifty": [], "Sensex": [], "Midcap": [], "FinNifty": []}, [], []

    watch_list = watch_list or []
    rows = []
    index_impact = {"Nifty": [], "Bank Nifty": [], "Sensex": [], "Midcap": [], "FinNifty": []}
    bullish_stocks, bearish_stocks = [], []

    for n in raw_news[:40]:
        sentiment, conf = classify_sentiment(n["headline"])
        category = classify_category(n["headline"])
        rows.append({
            "Time": n["time"] or "-",
            "Headline": n["headline"],
            "Category": category,
            "Sentiment": sentiment,
            "Confidence": f"{conf}%",
        })

        # naive index-impact fan-out — a genuinely market-wide headline (economy/global/RBI/US)
        # is treated as affecting all indices; company-specific news affects stocks only.
        if category in ("Economy", "RBI", "Global", "US Market", "SGX Gift Nifty", "Crude Oil"):
            for idx in index_impact:
                index_impact[idx].append((sentiment, conf))

        hits = extract_impacted_symbols(n["headline"], watch_list)
        if sentiment == "Bullish":
            bullish_stocks.extend(hits)
        elif sentiment == "Bearish":
            bearish_stocks.extend(hits)

    df = pd.DataFrame(rows)

    def color_sentiment(val):
        color = {"Bullish": "green", "Bearish": "red", "Neutral": "gray"}.get(val, "black")
        return f"color: {color}; font-weight: 600"

    st.dataframe(
        df.style.applymap(color_sentiment, subset=["Sentiment"]),
        use_container_width=True,
        height=380,
    )

    return rows, index_impact, list(set(bullish_stocks)), list(set(bearish_stocks))


def render_news_ai_analysis(index_impact):
    st.subheader("🧠 AI News Impact Analysis")
    cols = st.columns(len(index_impact))
    for col, (idx_name, signals) in zip(cols, index_impact.items()):
        with col:
            if not signals:
                st.metric(idx_name, "No signal")
                continue
            bull = sum(1 for s, c in signals if s == "Bullish")
            bear = sum(1 for s, c in signals if s == "Bearish")
            avg_conf = int(np.mean([c for _, c in signals])) if signals else 0
            if bull > bear:
                st.metric(idx_name, "Bullish", delta=f"{avg_conf}% conf")
            elif bear > bull:
                st.metric(idx_name, "Bearish", delta=f"-{avg_conf}% conf")
            else:
                st.metric(idx_name, "Neutral", delta=f"{avg_conf}% conf")


# =============================================================================
# TECHNICAL INDICATORS (self-contained, does not touch existing scanner code)
# =============================================================================

def ema(series: pd.Series, length: int):
    return series.ewm(span=length, adjust=False).mean()


def rsi(series: pd.Series, length: int = 14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / length, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return (100 - (100 / (1 + rs))).fillna(50)


def macd(series: pd.Series, fast=12, slow=26, signal=9):
    ema_fast = ema(series, fast)
    ema_slow = ema(series, slow)
    macd_line = ema_fast - ema_slow
    signal_line = ema(macd_line, signal)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def atr(df: pd.DataFrame, length: int = 14):
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / length, adjust=False).mean()


def adx(df: pd.DataFrame, length: int = 14):
    high, low, close = df["High"], df["Low"], df["Close"]
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    tr = atr(df, length)
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / length, adjust=False).mean() / tr.replace(0, np.nan)
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / length, adjust=False).mean() / tr.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / length, adjust=False).mean().fillna(0)


def vwap(df: pd.DataFrame):
    typical = (df["High"] + df["Low"] + df["Close"]) / 3
    cum_vp = (typical * df["Volume"]).cumsum()
    cum_vol = df["Volume"].cumsum().replace(0, np.nan)
    return (cum_vp / cum_vol).fillna(method="bfill")


def pivot_support_resistance(df: pd.DataFrame, lookback: int = 20):
    recent = df.tail(lookback)
    support = recent["Low"].min()
    resistance = recent["High"].max()
    return round(float(support), 2), round(float(resistance), 2)


def trend_from_price_ema(price, e20, e50, e200):
    if price > e20 > e50 > e200:
        return "Strong Uptrend"
    elif price > e50 and price > e200:
        return "Uptrend"
    elif price < e20 < e50 < e200:
        return "Strong Downtrend"
    elif price < e50 and price < e200:
        return "Downtrend"
    else:
        return "Sideways"


# =============================================================================
# SMART MONEY CONCEPTS (self-contained copy — separate from existing scanner)
# =============================================================================

def detect_swings(df: pd.DataFrame, window: int = 3):
    highs, lows = df["High"].values, df["Low"].values
    swing_high = np.zeros(len(df), dtype=bool)
    swing_low = np.zeros(len(df), dtype=bool)
    for i in range(window, len(df) - window):
        if highs[i] == max(highs[i - window:i + window + 1]):
            swing_high[i] = True
        if lows[i] == min(lows[i - window:i + window + 1]):
            swing_low[i] = True
    return swing_high, swing_low


def detect_bos_choch(df: pd.DataFrame, window: int = 3):
    """Simplified Break of Structure / Change of Character detection."""
    swing_high, swing_low = detect_swings(df, window)
    highs = df["High"].values
    lows = df["Low"].values
    closes = df["Close"].values

    last_swing_high = None
    last_swing_low = None
    structure = "Neutral"
    events = []

    for i in range(len(df)):
        if swing_high[i]:
            last_swing_high = highs[i]
        if swing_low[i]:
            last_swing_low = lows[i]

        if last_swing_high is not None and closes[i] > last_swing_high:
            if structure in ("Neutral", "Bearish"):
                events.append((df.index[i], "CHOCH", "Bullish"))
                structure = "Bullish"
            else:
                events.append((df.index[i], "BOS", "Bullish"))
            last_swing_high = None

        if last_swing_low is not None and closes[i] < last_swing_low:
            if structure in ("Neutral", "Bullish"):
                events.append((df.index[i], "CHOCH", "Bearish"))
                structure = "Bearish"
            else:
                events.append((df.index[i], "BOS", "Bearish"))
            last_swing_low = None

    return structure, events[-5:]


def detect_fvg(df: pd.DataFrame):
    """Fair Value Gap: 3-candle imbalance."""
    gaps = []
    highs, lows = df["High"].values, df["Low"].values
    for i in range(2, len(df)):
        if lows[i] > highs[i - 2]:
            gaps.append({"index": df.index[i], "type": "Bullish FVG",
                         "zone": (float(highs[i - 2]), float(lows[i]))})
        elif highs[i] < lows[i - 2]:
            gaps.append({"index": df.index[i], "type": "Bearish FVG",
                         "zone": (float(highs[i]), float(lows[i - 2]))})
    return gaps[-3:]


def detect_order_blocks(df: pd.DataFrame):
    """Simplified order block: last opposite candle before a strong impulse move."""
    closes = df["Close"].values
    opens = df["Open"].values
    blocks = []
    avg_range = (df["High"] - df["Low"]).mean()
    for i in range(1, len(df)):
        body = abs(closes[i] - opens[i])
        if body > avg_range * 1.5:
            impulse_up = closes[i] > opens[i]
            prev_bearish = closes[i - 1] < opens[i - 1]
            prev_bullish = closes[i - 1] > opens[i - 1]
            if impulse_up and prev_bearish:
                blocks.append({"index": df.index[i - 1], "type": "Bullish OB",
                               "zone": (float(df["Low"].iloc[i - 1]), float(df["High"].iloc[i - 1]))})
            elif (not impulse_up) and prev_bullish:
                blocks.append({"index": df.index[i - 1], "type": "Bearish OB",
                               "zone": (float(df["Low"].iloc[i - 1]), float(df["High"].iloc[i - 1]))})
    return blocks[-3:]


def detect_cisd(df: pd.DataFrame):
    """Change in State of Delivery: first close back through the prior opposite leg's open."""
    opens, closes = df["Open"].values, df["Close"].values
    signals = []
    for i in range(3, len(df)):
        was_bearish_leg = all(closes[j] < opens[j] for j in range(i - 3, i))
        if was_bearish_leg and closes[i] > opens[i - 3]:
            signals.append({"index": df.index[i], "type": "Bullish CISD"})
        was_bullish_leg = all(closes[j] > opens[j] for j in range(i - 3, i))
        if was_bullish_leg and closes[i] < opens[i - 3]:
            signals.append({"index": df.index[i], "type": "Bearish CISD"})
    return signals[-3:]


def detect_liquidity_zones(df: pd.DataFrame, window: int = 20):
    recent = df.tail(window)
    return {
        "buy_side_liquidity": round(float(recent["High"].max()), 2),
        "sell_side_liquidity": round(float(recent["Low"].min()), 2),
    }


# =============================================================================
# FYERS DATA HELPERS
# =============================================================================

@st.cache_data(ttl=60, show_spinner=False)
def get_history_df(_fyers, symbol, resolution="15", days_back=15):
    end = datetime.now()
    start = end - timedelta(days=days_back)
    try:
        resp = _fyers.history({
            "symbol": symbol,
            "resolution": resolution,
            "date_format": "1",
            "range_from": start.strftime("%Y-%m-%d"),
            "range_to": end.strftime("%Y-%m-%d"),
            "cont_flag": "1",
        })
        candles = resp.get("candles", [])
        if not candles:
            return None
        df = pd.DataFrame(candles, columns=["Timestamp", "Open", "High", "Low", "Close", "Volume"])
        df["Timestamp"] = pd.to_datetime(df["Timestamp"], unit="s")
        df.set_index("Timestamp", inplace=True)
        return df
    except Exception:
        return None


@st.cache_data(ttl=60, show_spinner=False)
def get_quote(_fyers, symbol):
    try:
        resp = _fyers.quotes({"symbols": symbol})
        if resp.get("s") == "ok":
            return resp["d"][0]["v"]
    except Exception:
        pass
    return None


@st.cache_data(ttl=60, show_spinner=False)
def get_option_chain(_fyers, symbol, strike_count=10):
    try:
        resp = _fyers.optionchain({"symbol": symbol, "strikecount": strike_count, "timestamp": ""})
        if resp.get("s") == "ok":
            return resp.get("data", {})
    except Exception:
        pass
    return None


def compute_pcr_and_writing(option_chain, prev_snapshot=None):
    """PCR + basic put/call writing detection from OI change vs previous snapshot."""
    if not option_chain or "optionsChain" not in option_chain:
        return None
    rows = option_chain["optionsChain"]
    total_ce_oi = sum(r.get("oi", 0) for r in rows if r.get("option_type") == "CE")
    total_pe_oi = sum(r.get("oi", 0) for r in rows if r.get("option_type") == "PE")
    pcr = round(total_pe_oi / total_ce_oi, 2) if total_ce_oi else 0

    put_writing, call_writing = "Unknown", "Unknown"
    if prev_snapshot:
        prev_ce = prev_snapshot.get("ce_oi", total_ce_oi)
        prev_pe = prev_snapshot.get("pe_oi", total_pe_oi)
        call_writing = "Active" if total_ce_oi > prev_ce else "Unwinding"
        put_writing = "Active" if total_pe_oi > prev_pe else "Unwinding"

    return {
        "pcr": pcr,
        "ce_oi": total_ce_oi,
        "pe_oi": total_pe_oi,
        "put_writing": put_writing,
        "call_writing": call_writing,
    }


# =============================================================================
# SECTION 4: INDEX ANALYSIS
# =============================================================================

def analyze_index(fyers, name, symbol):
    df = get_history_df(fyers, symbol, resolution="15", days_back=10)
    quote = get_quote(fyers, symbol)

    if df is None or len(df) < 25:
        return None

    price = float(quote.get("lp")) if quote else float(df["Close"].iloc[-1])
    e20 = float(ema(df["Close"], 20).iloc[-1])
    e50 = float(ema(df["Close"], 50).iloc[-1]) if len(df) >= 50 else e20
    e200 = float(ema(df["Close"], 200).iloc[-1]) if len(df) >= 200 else e50
    vw = float(vwap(df).iloc[-1])
    r = float(rsi(df["Close"]).iloc[-1])
    macd_line, signal_line, _ = macd(df["Close"])
    macd_val = float(macd_line.iloc[-1] - signal_line.iloc[-1])
    a = float(adx(df).iloc[-1])
    vol = float(df["Volume"].iloc[-1])
    support, resistance = pivot_support_resistance(df)
    trend = trend_from_price_ema(price, e20, e50, e200)

    prev_key = f"oc_{symbol}"
    prev_snapshot = st.session_state.get(prev_key)
    option_chain = get_option_chain(fyers, symbol)
    pcr_data = compute_pcr_and_writing(option_chain, prev_snapshot)
    if pcr_data:
        st.session_state[prev_key] = {"ce_oi": pcr_data["ce_oi"], "pe_oi": pcr_data["pe_oi"]}

    # crude probability model from RSI + trend + ADX strength
    bullish_score = 50
    bullish_score += (r - 50) * 0.6
    bullish_score += 10 if trend in ("Uptrend", "Strong Uptrend") else (-10 if trend in ("Downtrend", "Strong Downtrend") else 0)
    bullish_score += (a - 20) * 0.3 if macd_val > 0 else -(a - 20) * 0.3
    prob_up = int(min(95, max(5, bullish_score)))
    prob_down = 100 - prob_up

    return {
        "name": name, "price": round(price, 2), "trend": trend,
        "ema20": round(e20, 2), "ema50": round(e50, 2), "ema200": round(e200, 2),
        "vwap": round(vw, 2), "rsi": round(r, 1), "macd": round(macd_val, 2),
        "adx": round(a, 1), "volume": int(vol),
        "pcr": pcr_data["pcr"] if pcr_data else "N/A",
        "put_writing": pcr_data["put_writing"] if pcr_data else "N/A",
        "call_writing": pcr_data["call_writing"] if pcr_data else "N/A",
        "support": support, "resistance": resistance,
        "prob_up": prob_up, "prob_down": prob_down,
        "oi": pcr_data["ce_oi"] + pcr_data["pe_oi"] if pcr_data else "N/A",
    }


def render_index_analysis(fyers):
    st.subheader("📈 Index Analysis")
    results = {}
    tabs = st.tabs(list(INDEX_SYMBOLS.keys()))
    for tab, (name, symbol) in zip(tabs, INDEX_SYMBOLS.items()):
        with tab:
            data = analyze_index(fyers, name, symbol)
            if data is None:
                st.info(f"No data available for {name} right now.")
                continue
            results[name] = data

            trend_color = "green" if "Up" in data["trend"] else ("red" if "Down" in data["trend"] else "gray")
            c1, c2, c3 = st.columns(3)
            c1.metric("Price", data["price"])
            c2.markdown(f"**Trend:** :{trend_color}[{data['trend']}]")
            c3.metric("RSI", data["rsi"])

            c4, c5, c6, c7 = st.columns(4)
            c4.metric("EMA20", data["ema20"])
            c5.metric("EMA50", data["ema50"])
            c6.metric("EMA200", data["ema200"])
            c7.metric("VWAP", data["vwap"])

            c8, c9, c10, c11 = st.columns(4)
            c8.metric("MACD", data["macd"])
            c9.metric("ADX", data["adx"])
            c10.metric("Support", data["support"])
            c11.metric("Resistance", data["resistance"])

            c12, c13, c14 = st.columns(3)
            c12.metric("PCR", data["pcr"])
            c13.metric("Put Writing", data["put_writing"])
            c14.metric("Call Writing", data["call_writing"])

            st.progress(data["prob_up"] / 100, text=f"Probability Up: {data['prob_up']}% | Down: {data['prob_down']}%")
    return results


# =============================================================================
# SECTION 5: FUTURES ANALYSIS
# =============================================================================

def classify_futures_buildup(price_change, oi_change):
    if price_change > 0 and oi_change > 0:
        return "Long Build Up", "BUY"
    elif price_change < 0 and oi_change > 0:
        return "Short Build Up", "SELL"
    elif price_change > 0 and oi_change < 0:
        return "Short Covering", "BUY"
    elif price_change < 0 and oi_change < 0:
        return "Long Unwinding", "SELL"
    else:
        return "Neutral", "WAIT"


def analyze_futures(fyers, label, symbol):
    df = get_history_df(fyers, symbol, resolution="15", days_back=5)
    if df is None or len(df) < 2:
        return None
    price_change = float(df["Close"].iloc[-1] - df["Close"].iloc[-2])
    price_change_pct = round(price_change / df["Close"].iloc[-2] * 100, 2)
    volume = int(df["Volume"].iloc[-1])
    vol_change = int(df["Volume"].iloc[-1] - df["Volume"].iloc[-2])

    # Fyers history doesn't return OI directly for most plans; approximate
    # trend strength from volume + price change as a proxy, clearly labeled.
    oi_proxy_change = vol_change
    buildup, direction = classify_futures_buildup(price_change, oi_proxy_change)
    a = float(adx(df).iloc[-1]) if len(df) >= 15 else 0
    trend_strength = "Strong" if a > 25 else ("Moderate" if a > 15 else "Weak")

    return {
        "label": label, "price_change_pct": price_change_pct,
        "volume": volume, "buildup": buildup, "direction": direction,
        "trend_strength": trend_strength,
        "note": "OI change approximated from volume delta — connect Fyers OI feed for exact OI." ,
    }


def render_futures_analysis(fyers):
    st.subheader("⚡ Futures Analysis")
    fut_symbols = {
        "Nifty Futures": INDEX_SYMBOLS["NIFTY50"],
        "BankNifty Futures": INDEX_SYMBOLS["BANKNIFTY"],
        "FinNifty Futures": INDEX_SYMBOLS["FINNIFTY"],
    }
    cols = st.columns(3)
    for col, (label, sym) in zip(cols, fut_symbols.items()):
        with col:
            data = analyze_futures(fyers, label, sym)
            if not data:
                st.info(f"No data for {label}")
                continue
            color = "green" if data["direction"] == "BUY" else ("red" if data["direction"] == "SELL" else "gray")
            st.markdown(f"**{label}**")
            st.markdown(f"Price Δ: {data['price_change_pct']}%")
            st.markdown(f"Buildup: **{data['buildup']}**")
            st.markdown(f"Trend Strength: {data['trend_strength']}")
            st.markdown(f"AI Direction: :{color}[**{data['direction']}**]")
            st.caption(data["note"])


# =============================================================================
# SECTION 6: STOCK ANALYSIS
# =============================================================================

def analyze_stock(fyers, symbol):
    df = get_history_df(fyers, symbol, resolution="15", days_back=20)
    if df is None or len(df) < 30:
        return None

    price = float(df["Close"].iloc[-1])
    e20 = float(ema(df["Close"], 20).iloc[-1])
    e50 = float(ema(df["Close"], 50).iloc[-1])
    e200 = float(ema(df["Close"], 200).iloc[-1]) if len(df) >= 200 else e50
    r = float(rsi(df["Close"]).iloc[-1])
    macd_line, signal_line, _ = macd(df["Close"])
    macd_val = float(macd_line.iloc[-1] - signal_line.iloc[-1])
    vw = float(vwap(df).iloc[-1])
    a = float(adx(df).iloc[-1])
    at = float(atr(df).iloc[-1])
    support, resistance = pivot_support_resistance(df)
    trend = trend_from_price_ema(price, e20, e50, e200)

    avg_vol = df["Volume"].tail(20).mean()
    rel_vol = round(df["Volume"].iloc[-1] / avg_vol, 2) if avg_vol else 1.0
    volume_spike = rel_vol > 2.0

    golden_cross = e50 > e200 and float(ema(df["Close"], 50).iloc[-2]) <= float(ema(df["Close"], 200).iloc[-2]) if len(df) >= 200 else False
    death_cross = e50 < e200 and float(ema(df["Close"], 50).iloc[-2]) >= float(ema(df["Close"], 200).iloc[-2]) if len(df) >= 200 else False

    breakout = price > resistance
    breakdown = price < support

    structure, bos_choch_events = detect_bos_choch(df)
    fvg = detect_fvg(df)
    obs = detect_order_blocks(df)
    cisd = detect_cisd(df)
    liquidity = detect_liquidity_zones(df)

    institutional_buying = volume_spike and price > df["Open"].iloc[-1]
    institutional_selling = volume_spike and price < df["Open"].iloc[-1]

    # Composite probability
    score = 50
    score += (r - 50) * 0.5
    score += 15 if trend in ("Uptrend", "Strong Uptrend") else (-15 if trend in ("Downtrend", "Strong Downtrend") else 0)
    score += 10 if macd_val > 0 else -10
    score += 10 if breakout else (-10 if breakdown else 0)
    score += 8 if institutional_buying else (-8 if institutional_selling else 0)
    buy_pct = int(min(95, max(5, score)))
    sell_pct = 100 - buy_pct

    return {
        "symbol": symbol, "price": round(price, 2), "trend": trend,
        "momentum": "Positive" if macd_val > 0 else "Negative",
        "strength": "Strong" if a > 25 else ("Moderate" if a > 15 else "Weak"),
        "rel_volume": rel_vol, "delivery_pct": None,  # requires bhavcopy/delivery data source
        "breakout": breakout, "breakdown": breakdown,
        "golden_cross": golden_cross, "death_cross": death_cross,
        "rsi": round(r, 1), "macd": round(macd_val, 2), "vwap": round(vw, 2),
        "ema20": round(e20, 2), "ema50": round(e50, 2), "ema200": round(e200, 2),
        "adx": round(a, 1), "atr": round(at, 2),
        "support": support, "resistance": resistance,
        "order_blocks": obs, "fvg": fvg, "liquidity_zone": liquidity,
        "bos_choch": bos_choch_events, "structure": structure, "cisd": cisd,
        "volume_spike": volume_spike,
        "institutional_buying": institutional_buying,
        "institutional_selling": institutional_selling,
        "buy_pct": buy_pct, "sell_pct": sell_pct,
    }


def render_stock_analysis(fyers, watch_list):
    st.subheader("🔍 Stock Analysis")
    if not watch_list:
        st.info("Add symbols to your scanner watch-list to see per-stock AI analysis here.")
        return {}

    selected = st.multiselect("Select stocks to analyze", watch_list, default=watch_list[:3])
    results = {}
    for sym in selected:
        data = analyze_stock(fyers, sym)
        if data is None:
            st.warning(f"No sufficient data for {sym}")
            continue
        results[sym] = data
        with st.expander(f"{sym} — {data['trend']} | BUY {data['buy_pct']}% / SELL {data['sell_pct']}%"):
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Price", data["price"])
            c2.metric("RSI", data["rsi"])
            c3.metric("ADX", data["adx"])
            c4.metric("ATR", data["atr"])

            c5, c6, c7 = st.columns(3)
            c5.metric("Support", data["support"])
            c6.metric("Resistance", data["resistance"])
            c7.metric("Rel. Volume", data["rel_volume"])

            flags = []
            if data["breakout"]: flags.append("🟢 Breakout")
            if data["breakdown"]: flags.append("🔴 Breakdown")
            if data["golden_cross"]: flags.append("🟡 Golden Cross")
            if data["death_cross"]: flags.append("⚫ Death Cross")
            if data["volume_spike"]: flags.append("📊 Volume Spike")
            if data["institutional_buying"]: flags.append("🏦 Institutional Buying")
            if data["institutional_selling"]: flags.append("🏦 Institutional Selling")
            if flags:
                st.markdown(" | ".join(flags))

            st.markdown(f"**Market Structure:** {data['structure']}")
            if data["order_blocks"]:
                st.caption(f"Order Blocks: {data['order_blocks']}")
            if data["fvg"]:
                st.caption(f"Fair Value Gaps: {data['fvg']}")
            if data["cisd"]:
                st.caption(f"CISD signals: {data['cisd']}")
            st.caption(f"Liquidity Zone: {data['liquidity_zone']}")

            st.progress(data["buy_pct"] / 100, text=f"BUY {data['buy_pct']}% / SELL {data['sell_pct']}%")
    return results


# =============================================================================
# SECTION 7: AI PREDICTION ENGINE
# =============================================================================

def generate_ai_prediction(stock_data, news_bullish, news_bearish, fii_dii_sentiment):
    """
    Transparent weighted composite — combines price action, indicators,
    news, FII/DII, and smart-money structure into one signal.
    """
    sym = stock_data["symbol"]
    score = 0

    # Price action / trend (weight 25)
    trend_map = {"Strong Uptrend": 25, "Uptrend": 15, "Sideways": 0, "Downtrend": -15, "Strong Downtrend": -25}
    score += trend_map.get(stock_data["trend"], 0)

    # Indicators (weight 20)
    score += 10 if stock_data["macd"] > 0 else -10
    score += 10 if stock_data["rsi"] > 55 else (-10 if stock_data["rsi"] < 45 else 0)

    # Structure / SMC (weight 20)
    if stock_data["structure"] == "Bullish":
        score += 15
    elif stock_data["structure"] == "Bearish":
        score -= 15
    if stock_data["breakout"]:
        score += 10
    if stock_data["breakdown"]:
        score -= 10

    # News (weight 15)
    if sym.split(":")[-1].replace("-EQ", "") in news_bullish:
        score += 15
    if sym.split(":")[-1].replace("-EQ", "") in news_bearish:
        score -= 15

    # FII/DII broad market sentiment (weight 10)
    fii_map = {"Very Bullish": 10, "Bullish": 6, "Neutral": 0, "Bearish": -6, "Very Bearish": -10}
    score += fii_map.get(fii_dii_sentiment, 0)

    # Institutional activity (weight 10)
    if stock_data["institutional_buying"]:
        score += 10
    if stock_data["institutional_selling"]:
        score -= 10

    score = max(-100, min(100, score))
    confidence = int(50 + abs(score) / 2)

    if score >= 40:
        signal = "STRONG BUY"
    elif score >= 15:
        signal = "BUY"
    elif score > -15:
        signal = "WATCH"
    elif score > -40:
        signal = "SELL"
    else:
        signal = "STRONG SELL"

    price = stock_data["price"]
    at = stock_data["atr"] or (price * 0.01)
    if "BUY" in signal:
        target1 = round(price + at * 1.5, 2)
        target2 = round(price + at * 3, 2)
        stop_loss = round(price - at * 1.2, 2)
    elif "SELL" in signal:
        target1 = round(price - at * 1.5, 2)
        target2 = round(price - at * 3, 2)
        stop_loss = round(price + at * 1.2, 2)
    else:
        target1 = target2 = stop_loss = price

    risk = abs(price - stop_loss)
    reward = abs(target1 - price)
    rr = round(reward / risk, 2) if risk else 0

    return {
        "symbol": sym, "signal": signal, "confidence": confidence,
        "target1": target1, "target2": target2, "stop_loss": stop_loss,
        "risk_reward": rr, "score": score,
    }


def render_ai_prediction(stock_results, news_bullish, news_bearish, fii_dii_sentiment):
    st.subheader("🤖 AI Prediction Engine")
    if not stock_results:
        st.info("Run Stock Analysis above first to generate AI predictions.")
        return []

    predictions = []
    for sym, data in stock_results.items():
        pred = generate_ai_prediction(data, news_bullish, news_bearish, fii_dii_sentiment)
        predictions.append(pred)

    df = pd.DataFrame(predictions)

    def color_signal(val):
        colors = {
            "STRONG BUY": "background-color: #0a5c0a; color: white",
            "BUY": "background-color: #1e8f1e; color: white",
            "WATCH": "background-color: #b8860b; color: white",
            "SELL": "background-color: #b03030; color: white",
            "STRONG SELL": "background-color: #7a0d0d; color: white",
        }
        return colors.get(val, "")

    st.dataframe(
        df.style.applymap(color_signal, subset=["signal"]),
        use_container_width=True,
    )
    return predictions


# =============================================================================
# SECTION 8: DASHBOARD
# =============================================================================

def render_dashboard(fii_dii, index_results, news_rows, predictions):
    st.subheader("🖥️ AI Market Dashboard")

    nifty_trend = index_results.get("NIFTY50", {}).get("trend", "N/A")
    bn_trend = index_results.get("BANKNIFTY", {}).get("trend", "N/A")
    vix_price = index_results.get("VIX", {}).get("price", "N/A")

    active_buys = sum(1 for p in predictions if "BUY" in p["signal"])
    active_sells = sum(1 for p in predictions if "SELL" in p["signal"])

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Nifty Trend", nifty_trend)
    c2.metric("BankNifty Trend", bn_trend)
    c3.metric("Market Sentiment", fii_dii.get("sentiment", "N/A"))
    c4.metric("India VIX", vix_price)

    c5, c6, c7, c8 = st.columns(4)
    c5.metric("Net FII (₹Cr)", f"{fii_dii.get('net_fii', 0):,.0f}")
    c6.metric("Net DII (₹Cr)", f"{fii_dii.get('net_dii', 0):,.0f}")
    c7.metric("Live News Count", len(news_rows))
    c8.metric("Active Signals", f"🟢{active_buys} / 🔴{active_sells}")


# =============================================================================
# SECTION 9: AUTO REFRESH
# =============================================================================

def setup_autorefresh(interval_seconds=60):
    if _HAS_AUTOREFRESH:
        st_autorefresh(interval=interval_seconds * 1000, key="ai_market_intel_autorefresh")
    else:
        # Fallback: lightweight manual refresh timer using session_state,
        # avoids a hard dependency on streamlit-autorefresh.
        last = st.session_state.get("_ai_mi_last_refresh", 0)
        now = time.time()
        st.caption(
            f"Auto-refresh: install `streamlit-autorefresh` for seamless 60s refresh. "
            f"Click 'Refresh Now' below or reload the page."
        )
        if st.button("🔄 Refresh Now"):
            st.session_state["_ai_mi_last_refresh"] = now
            st.cache_data.clear()
            st.rerun()


# =============================================================================
# MAIN ENTRY POINT
# =============================================================================

def show_ai_market_intelligence(fyers, watch_list=None):
    """
    Call this from your main app, e.g. inside a new tab:
        show_ai_market_intelligence(fyers, watch_list=["NSE:RELIANCE-EQ", ...])

    Does not read, import, or modify any existing scanner module.
    """
    st.title("🧠 AI Market Intelligence")
    setup_autorefresh(60)

    watch_list = watch_list or []

    tabs = st.tabs([
        "Dashboard", "FII/DII", "Live News", "News AI Analysis",
        "Index Analysis", "Futures Analysis", "Stock Analysis", "AI Prediction",
    ])

    with tabs[1]:
        fii_dii = render_fii_dii_card()

    with tabs[2]:
        news_rows, index_impact, news_bullish, news_bearish = render_news_panel(watch_list)

    with tabs[3]:
        render_news_ai_analysis(index_impact)

    with tabs[4]:
        index_results = render_index_analysis(fyers)

    with tabs[5]:
        render_futures_analysis(fyers)

    with tabs[6]:
        stock_results = render_stock_analysis(fyers, watch_list)

    with tabs[7]:
        predictions = render_ai_prediction(
            stock_results, news_bullish, news_bearish, fii_dii.get("sentiment", "Neutral")
        )

    with tabs[0]:
        render_dashboard(fii_dii, index_results, news_rows, predictions)
