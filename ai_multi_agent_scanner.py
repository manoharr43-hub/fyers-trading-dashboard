"""
AI Multi-Agent Scanner for NSE AI PRO V17
Yahoo Finance batch edition.

- Uses scanner.py only to load NSE/F&O symbol universes.
- Uses Yahoo Finance for all candle data; no FYERS history/quotes/orders.
- Batch-downloads multiple symbols per interval to reduce request count.
- Runs 12 OHLCV/chart-pattern agents on 5M, 15M and 1H completed candles.
- Adds a separate Yahoo Finance news-context agent for shortlisted stocks.
- Keeps show_ai_multi_agent_scanner(client=None) for app.py compatibility.

Add to requirements.txt:
    yfinance>=0.2.50
    openpyxl>=3.1.0
"""

import concurrent.futures
import io
import math
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import streamlit as st

try:
    import yfinance as yf
except Exception:
    yf = None

IST = ZoneInfo("Asia/Kolkata")

INDEX_NAMES = {
    "NIFTY", "NIFTY50", "BANKNIFTY", "NIFTYBANK", "FINNIFTY",
    "MIDCPNIFTY", "NIFTYNEXT50", "SENSEX", "BANKEX",
}
TIMEFRAMES = {
    "5M": ("5m", 5),
    "15M": ("15m", 15),
    "1H": ("60m", 60),
}
AGENT_NAMES = [
    "Accumulation", "Distribution", "Liquidity Sweep", "Volume Expansion",
    "VWAP", "EMA Trend", "RSI Momentum", "Fair Value Gap",
    "HH/HL Structure", "LH/LL Structure", "Pre-Big Movement",
    "Chart Pattern",
]


def _normalise_symbols(values):
    """Accept a list/tuple/set or a DataFrame returned by scanner.py."""
    if values is None:
        return []
    if isinstance(values, pd.DataFrame):
        for col in ("symbol", "Symbol", "fyers_symbol", "tradingsymbol"):
            if col in values.columns:
                values = values[col].dropna().astype(str).tolist()
                break
        else:
            return []
    if isinstance(values, dict):
        values = values.get("symbols", values.get("data", []))
    result = []
    for item in values:
        if isinstance(item, dict):
            item = (
                item.get("symbol") or item.get("Symbol")
                or item.get("fyers_symbol") or item.get("tradingsymbol")
            )
        if item:
            symbol = str(item).strip()
            if symbol and symbol not in result:
                result.append(symbol)
    return result


def _is_index_symbol(symbol):
    text = str(symbol).upper().replace("-INDEX", "").replace("NSE:", "")
    token = text.split("-")[0].split("_")[0]
    return token in INDEX_NAMES or any(token.startswith(name) for name in INDEX_NAMES)


def _to_yahoo_symbol(symbol):
    value = str(symbol).strip().upper()
    value = value.replace("NSE:", "").replace("BSE:", "")
    value = value.replace("-EQ", "").replace("-BE", "").replace("_EQ", "")
    if value.endswith(".NS") or value.endswith(".BO"):
        return value
    return f"{value}.NS"


def _closed_candles(df, minutes):
    """Return completed OHLCV candles, converted to IST."""
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()

    out = df.copy()
    if "Time" not in out.columns:
        out["Time"] = out.index

    out["Time"] = pd.to_datetime(out["Time"], errors="coerce")
    out = out.dropna(subset=["Time"]).sort_values("Time")
    if out.empty:
        return pd.DataFrame()

    times = out["Time"]
    try:
        if times.dt.tz is None:
            times = times.dt.tz_localize(
                IST, ambiguous="NaT", nonexistent="shift_forward"
            )
        else:
            times = times.dt.tz_convert(IST)
    except (TypeError, AttributeError):
        return pd.DataFrame()

    out["Time"] = times
    now = datetime.now(IST)
    out = out.loc[(out["Time"] + pd.Timedelta(minutes=minutes)) <= now].copy()

    rename = {}
    for col in out.columns:
        name = str(col).strip().lower()
        if name in ("open", "high", "low", "close", "volume"):
            rename[col] = name.capitalize()
    out = out.rename(columns=rename)

    required = ["Open", "High", "Low", "Close", "Volume"]
    if any(col not in out.columns for col in required):
        return pd.DataFrame()

    for col in required:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=required)
    out = out[(out["Close"] > 0) & (out["High"] >= out["Low"])]
    return out.reset_index(drop=True)


@st.cache_data(ttl=60, show_spinner=False)
def _download_yahoo_batch_cached(tickers_tuple, interval, period):
    """Cached Yahoo batch download. Cache key includes the exact ticker batch."""
    if yf is None:
        raise RuntimeError("yfinance is not installed. Add yfinance>=0.2.50.")
    tickers = list(tickers_tuple)
    data = yf.download(
        tickers=tickers,
        period=period,
        interval=interval,
        auto_adjust=False,
        actions=False,
        progress=False,
        threads=False,
        group_by="ticker",
        timeout=25,
    )
    if data is None or data.empty:
        raise RuntimeError(f"Yahoo returned no data for this {interval} batch.")
    return data


def _extract_one_ticker(batch_df, ticker):
    """Extract one ticker from either Yahoo MultiIndex orientation."""
    if batch_df is None or batch_df.empty:
        return pd.DataFrame()

    if not isinstance(batch_df.columns, pd.MultiIndex):
        # Yahoo can return flat columns for a one-ticker batch.
        return batch_df.copy()

    cols = batch_df.columns
    for level in range(cols.nlevels):
        values = set(map(str, cols.get_level_values(level).unique()))
        if ticker in values:
            try:
                extracted = batch_df.xs(ticker, axis=1, level=level, drop_level=True)
                if isinstance(extracted.columns, pd.MultiIndex):
                    extracted.columns = extracted.columns.get_level_values(-1)
                return extracted
            except Exception:
                pass

    # Some yfinance versions normalize ticker labels differently.
    target = ticker.upper()
    for level in range(cols.nlevels):
        for value in cols.get_level_values(level).unique():
            if str(value).upper() == target:
                try:
                    return batch_df.xs(value, axis=1, level=level, drop_level=True)
                except Exception:
                    pass
    return pd.DataFrame()


def _safe_num(value, default=0.0):
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except Exception:
        return default


def _rsi(close, period=14):
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _vwap_last_session(df):
    """Approximate current-session VWAP from intraday OHLCV candles."""
    if df.empty:
        return np.nan
    x = df.copy()
    x["_date"] = x["Time"].dt.date
    last_date = x["_date"].iloc[-1]
    x = x[x["_date"] == last_date]
    typical = (x["High"] + x["Low"] + x["Close"]) / 3
    volume = x["Volume"].clip(lower=0)
    total = volume.sum()
    if total <= 0:
        return np.nan
    return _safe_num((typical * volume).sum() / total, np.nan)


def _agent_result(direction="WAIT", score=0, reason="No clear setup"):
    return {"direction": direction, "score": int(score), "reason": reason}


def _run_12_agents(df):
    """
    Run 12 deterministic OHLCV/chart agents on one timeframe.
    Scores are directional evidence, not probabilities.
    """
    if df is None or len(df) < 30:
        return {
            name: _agent_result("NO DATA", 0, "At least 30 completed candles required")
            for name in AGENT_NAMES
        }

    d = df.copy().reset_index(drop=True)
    o, h, l, c, v = (d[k].astype(float) for k in
                     ("Open", "High", "Low", "Close", "Volume"))
    last = _safe_num(c.iloc[-1])
    if last <= 0:
        return {name: _agent_result("NO DATA", 0, "Invalid last close") for name in AGENT_NAMES}

    ema9 = c.ewm(span=9, adjust=False).mean()
    ema21 = c.ewm(span=21, adjust=False).mean()
    ema50 = c.ewm(span=50, adjust=False).mean()
    rsi = _rsi(c)
    avg_vol = v.shift(1).rolling(20, min_periods=10).mean()
    rvol = _safe_num(v.iloc[-1] / avg_vol.iloc[-1]) if _safe_num(avg_vol.iloc[-1]) > 0 else 0
    atr = (h - l).rolling(14, min_periods=5).mean()
    atr_now = _safe_num(atr.iloc[-1])
    results = {}

    # 1. Accumulation: compact recent range, close in upper part, positive volume.
    recent = d.iloc[-10:]
    range_pct = (_safe_num(recent["High"].max()) - _safe_num(recent["Low"].min())) / last * 100
    close_location = (last - _safe_num(l.iloc[-1])) / max(
        _safe_num(h.iloc[-1] - l.iloc[-1]), 1e-9
    )
    if range_pct <= 3.0 and close_location >= 0.65 and last >= _safe_num(c.iloc[-5]):
        results["Accumulation"] = _agent_result("BUY", 1, "Tight range with close near candle high")
    else:
        results["Accumulation"] = _agent_result("WAIT", 0, "No clear accumulation pattern")

    # 2. Distribution: compact range, close in lower part, weak close.
    if range_pct <= 3.0 and close_location <= 0.35 and last <= _safe_num(c.iloc[-5]):
        results["Distribution"] = _agent_result("SELL", -1, "Tight range with close near candle low")
    else:
        results["Distribution"] = _agent_result("WAIT", 0, "No clear distribution pattern")

    # 3. Liquidity sweep: breach prior 10-bar extreme and close back inside.
    prior_high = _safe_num(h.iloc[-11:-1].max())
    prior_low = _safe_num(l.iloc[-11:-1].min())
    if _safe_num(h.iloc[-1]) > prior_high and last < prior_high:
        results["Liquidity Sweep"] = _agent_result("SELL", -2, "Swept prior highs and closed back below")
    elif _safe_num(l.iloc[-1]) < prior_low and last > prior_low:
        results["Liquidity Sweep"] = _agent_result("BUY", 2, "Swept prior lows and closed back above")
    else:
        results["Liquidity Sweep"] = _agent_result("WAIT", 0, "No confirmed 10-candle liquidity sweep")

    # 4. Volume expansion.
    if rvol >= 1.8:
        change = _safe_num(c.iloc[-1] - o.iloc[-1])
        if change > 0:
            results["Volume Expansion"] = _agent_result("BUY", 2, f"Volume {rvol:.2f}x with bullish candle")
        elif change < 0:
            results["Volume Expansion"] = _agent_result("SELL", -2, f"Volume {rvol:.2f}x with bearish candle")
        else:
            results["Volume Expansion"] = _agent_result("WAIT", 0, f"Volume {rvol:.2f}x but candle is flat")
    else:
        results["Volume Expansion"] = _agent_result("WAIT", 0, f"RVOL {rvol:.2f}x; expansion not confirmed")

    # 5. VWAP.
    vwap = _vwap_last_session(d)
    if pd.notna(vwap):
        if last > vwap * 1.001:
            results["VWAP"] = _agent_result("BUY", 1, f"Price above session VWAP ({vwap:.2f})")
        elif last < vwap * 0.999:
            results["VWAP"] = _agent_result("SELL", -1, f"Price below session VWAP ({vwap:.2f})")
        else:
            results["VWAP"] = _agent_result("WAIT", 0, "Price near VWAP")
    else:
        results["VWAP"] = _agent_result("NO DATA", 0, "VWAP volume unavailable")

    # 6. EMA trend.
    if ema9.iloc[-1] > ema21.iloc[-1] and last > ema50.iloc[-1]:
        results["EMA Trend"] = _agent_result("BUY", 2, "EMA9 > EMA21 and price > EMA50")
    elif ema9.iloc[-1] < ema21.iloc[-1] and last < ema50.iloc[-1]:
        results["EMA Trend"] = _agent_result("SELL", -2, "EMA9 < EMA21 and price < EMA50")
    else:
        results["EMA Trend"] = _agent_result("WAIT", 0, "EMA trend mixed")

    # 7. RSI momentum.
    rsi_now = _safe_num(rsi.iloc[-1], 50)
    if rsi_now >= 55:
        results["RSI Momentum"] = _agent_result("BUY", 1, f"RSI bullish ({rsi_now:.1f})")
    elif rsi_now <= 45:
        results["RSI Momentum"] = _agent_result("SELL", -1, f"RSI bearish ({rsi_now:.1f})")
    else:
        results["RSI Momentum"] = _agent_result("WAIT", 0, f"RSI neutral ({rsi_now:.1f})")

    # 8. Three-candle Fair Value Gap.
    if len(d) >= 3 and _safe_num(l.iloc[-1]) > _safe_num(h.iloc[-3]):
        results["Fair Value Gap"] = _agent_result("BUY", 1, "Bullish 3-candle FVG detected")
    elif len(d) >= 3 and _safe_num(h.iloc[-1]) < _safe_num(l.iloc[-3]):
        results["Fair Value Gap"] = _agent_result("SELL", -1, "Bearish 3-candle FVG detected")
    else:
        results["Fair Value Gap"] = _agent_result("WAIT", 0, "No fresh 3-candle FVG")

    # 9/10. Compare two recent swing windows for structure direction.
    old = d.iloc[-10:-5]
    new = d.iloc[-5:]
    old_high, new_high = _safe_num(old["High"].max()), _safe_num(new["High"].max())
    old_low, new_low = _safe_num(old["Low"].min()), _safe_num(new["Low"].min())
    if new_high > old_high and new_low > old_low:
        results["HH/HL Structure"] = _agent_result("BUY", 2, "Recent window made higher high and higher low")
    else:
        results["HH/HL Structure"] = _agent_result("WAIT", 0, "HH/HL not confirmed")
    if new_high < old_high and new_low < old_low:
        results["LH/LL Structure"] = _agent_result("SELL", -2, "Recent window made lower high and lower low")
    else:
        results["LH/LL Structure"] = _agent_result("WAIT", 0, "LH/LL not confirmed")

    # 11. Pre-big movement: volatility contraction + rising relative volume
    # + close near range edge. This is a watch signal, not a prediction.
    recent_range = _safe_num(h.iloc[-8:].max() - l.iloc[-8:].min())
    prior_range = _safe_num(h.iloc[-20:-8].max() - l.iloc[-20:-8].min())
    contraction = prior_range > 0 and recent_range < prior_range * 0.70
    near_high = last >= _safe_num(h.iloc[-8:].max()) * 0.995
    near_low = last <= _safe_num(l.iloc[-8:].min()) * 1.005
    if contraction and rvol >= 1.2 and near_high:
        results["Pre-Big Movement"] = _agent_result("PRE-BIG BUY", 2, "Range contraction + rising volume near range high")
    elif contraction and rvol >= 1.2 and near_low:
        results["Pre-Big Movement"] = _agent_result("PRE-BIG SELL", -2, "Range contraction + rising volume near range low")
    else:
        results["Pre-Big Movement"] = _agent_result("WAIT", 0, "Pre-movement conditions not aligned")

    # 12. Chart pattern / price-action agent (OHLCV-derived, not image recognition).
    prev_o, prev_c = _safe_num(o.iloc[-2]), _safe_num(c.iloc[-2])
    last_o, last_c = _safe_num(o.iloc[-1]), _safe_num(c.iloc[-1])
    last_h, last_l = _safe_num(h.iloc[-1]), _safe_num(l.iloc[-1])
    body = abs(last_c - last_o)
    candle_range = max(last_h - last_l, 1e-9)
    upper_wick = last_h - max(last_o, last_c)
    lower_wick = min(last_o, last_c) - last_l
    bullish_engulf = (
        prev_c < prev_o and last_c > last_o
        and last_o <= prev_c and last_c >= prev_o
    )
    bearish_engulf = (
        prev_c > prev_o and last_c < last_o
        and last_o >= prev_c and last_c <= prev_o
    )
    hammer = lower_wick >= max(body * 2.0, candle_range * 0.45) and upper_wick <= candle_range * 0.25
    shooting_star = upper_wick >= max(body * 2.0, candle_range * 0.45) and lower_wick <= candle_range * 0.25
    resistance = _safe_num(h.iloc[-21:-1].max())
    support = _safe_num(l.iloc[-21:-1].min())
    if bullish_engulf or (hammer and last_c > last_o):
        results["Chart Pattern"] = _agent_result("BUY", 2, "Bullish engulfing/hammer candle pattern")
    elif bearish_engulf or (shooting_star and last_c < last_o):
        results["Chart Pattern"] = _agent_result("SELL", -2, "Bearish engulfing/shooting-star candle pattern")
    elif last_c > resistance and rvol >= 1.2:
        results["Chart Pattern"] = _agent_result("BUY", 2, "Close broke 20-candle resistance with volume")
    elif last_c < support and rvol >= 1.2:
        results["Chart Pattern"] = _agent_result("SELL", -2, "Close broke 20-candle support with volume")
    else:
        results["Chart Pattern"] = _agent_result("WAIT", 0, "No confirmed candle pattern or volume-backed range break")

    return results


@st.cache_data(ttl=300, show_spinner=False)
def _fetch_yahoo_news_cached(yahoo_symbol):
    """Fetch a small news bundle for one shortlisted ticker; Yahoo coverage varies."""
    if yf is None:
        return []
    try:
        raw = yf.Ticker(yahoo_symbol).news or []
        cleaned = []
        for item in raw[:8]:
            content = item.get("content", item) if isinstance(item, dict) else {}
            title = content.get("title") or item.get("title") or ""
            publisher = content.get("provider", {}).get("displayName", "") if isinstance(content.get("provider"), dict) else content.get("publisher", "")
            pub_time = content.get("pubDate") or item.get("providerPublishTime")
            if isinstance(pub_time, str):
                try:
                    published = pd.Timestamp(pub_time)
                except Exception:
                    published = pd.NaT
            else:
                published = pd.to_datetime(pub_time, unit="s", errors="coerce") if pub_time else pd.NaT
            if title:
                cleaned.append({"title": str(title), "publisher": str(publisher), "published": published})
        return cleaned
    except Exception:
        return []


def _news_agent(yahoo_symbol):
    """Simple keyword sentiment context; not an LLM and not a trade signal by itself."""
    items = _fetch_yahoo_news_cached(yahoo_symbol)
    if not items:
        return {"direction": "NO NEWS", "score": 0, "reason": "Yahoo Finance returned no usable news"}
    positive = ("profit", "growth", "upgrade", "order win", "approval", "record", "buyback",
                "dividend", "partnership", "strong results", "beats estimates")
    negative = ("loss", "downgrade", "fraud", "default", "probe", "ban", "penalty",
                "resignation", "weak results", "misses estimates", "debt")
    now = pd.Timestamp.now(tz="UTC")
    scored = []
    for item in items:
        title = item["title"]
        text = title.lower()
        score = sum(word in text for word in positive) - sum(word in text for word in negative)
        published = item.get("published")
        age_hours = None
        if published is not None and not pd.isna(published):
            stamp = pd.Timestamp(published)
            if stamp.tzinfo is None:
                stamp = stamp.tz_localize("UTC")
            age_hours = max(0.0, (now - stamp.tz_convert("UTC")).total_seconds() / 3600)
        if age_hours is not None and age_hours > 48:
            score = 0
        scored.append((score, title, age_hours, item.get("publisher", "")))
    total = sum(x[0] for x in scored)
    newest = next((x for x in scored if x[2] is not None), scored[0])
    titles = " | ".join(x[1][:110] for x in scored[:3])
    if total > 0:
        direction, value = "POSITIVE NEWS", 1
    elif total < 0:
        direction, value = "NEGATIVE NEWS", -1
    else:
        direction, value = "NEUTRAL NEWS", 0
    age_text = f"; newest item {newest[2]:.1f}h old" if newest[2] is not None else ""
    return {"direction": direction, "score": value, "reason": f"Keyword-based headline context{age_text}: {titles}"}


def _analyse_timeframe(df):
    agents = _run_12_agents(df)
    scores = [a["score"] for a in agents.values()]
    total_score = int(sum(scores))
    buy_votes = sum(1 for a in agents.values() if a["direction"] in ("BUY", "PRE-BIG BUY"))
    sell_votes = sum(1 for a in agents.values() if a["direction"] in ("SELL", "PRE-BIG SELL"))
    valid = sum(1 for a in agents.values() if a["direction"] != "NO DATA")

    if valid < 8:
        direction = "NO DATA"
    elif total_score >= 5 and buy_votes >= 3:
        direction = "BUY"
    elif total_score <= -5 and sell_votes >= 3:
        direction = "SELL"
    else:
        direction = "WAIT"

    return {
        "direction": direction,
        "score": total_score,
        "buy_votes": buy_votes,
        "sell_votes": sell_votes,
        "agents": agents,
        "close": round(_safe_num(df["Close"].iloc[-1]), 2) if not df.empty else None,
        "rsi": round(_safe_num(_rsi(df["Close"].astype(float)).iloc[-1]), 2) if len(df) >= 15 else None,
    }


def _batch_download_and_analyse(symbol_items, batch_size, period, retry_count=1):
    """
    Download each interval in batches. Returns one result row per original symbol.
    symbol_items: [(original_symbol, yahoo_ticker), ...]
    """
    output = {}
    batches = [
        symbol_items[i:i + batch_size]
        for i in range(0, len(symbol_items), batch_size)
    ]

    def process_batch(batch):
        batch_result = {}
        tickers = [ticker for _, ticker in batch]
        for label, (interval, minutes) in TIMEFRAMES.items():
            raw = None
            last_error = ""
            for attempt in range(retry_count + 1):
                try:
                    raw = _download_yahoo_batch_cached(tuple(tickers), interval, period)
                    last_error = ""
                    break
                except Exception as exc:
                    last_error = str(exc)
                    if attempt < retry_count:
                        time.sleep(1.5 * (attempt + 1))
            for original, ticker in batch:
                one = _extract_one_ticker(raw, ticker) if raw is not None else pd.DataFrame()
                clean = _closed_candles(one, minutes)
                batch_result.setdefault(original, {"frames": {}, "errors": []})
                batch_result[original]["frames"][label] = clean
                if clean.empty:
                    message = f"{label}: {last_error or 'no completed candles / ticker missing in batch'}"
                    batch_result[original]["errors"].append(message)
        return batch_result

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(process_batch, batch) for batch in batches]
        for future in concurrent.futures.as_completed(futures):
            try:
                output.update(future.result())
            except Exception as exc:
                # A failed batch must not stop all other batches.
                for original, _ in batches[futures.index(future)]:
                    output[original] = {
                        "frames": {tf: pd.DataFrame() for tf in TIMEFRAMES},
                        "errors": [f"Batch error: {exc}"],
                    }
    return output, len(batches)


def _make_result_row(symbol, yahoo_symbol, payload):
    frames = payload.get("frames", {})
    analyses = {
        tf: _analyse_timeframe(frames.get(tf, pd.DataFrame()))
        for tf in TIMEFRAMES
    }
    valid = all(analyses[tf]["direction"] != "NO DATA" for tf in TIMEFRAMES)

    # 15M is the primary timeframe; 1H is trend confirmation; 5M is entry timing.
    if not valid:
        final_signal = "NO DATA"
        confidence = 0
        weighted = 0
    else:
        weights = {"5M": 0.25, "15M": 0.45, "1H": 0.30}
        weighted = sum(analyses[tf]["score"] * weights[tf] for tf in weights)
        buy_votes = sum(analyses[tf]["direction"] == "BUY" for tf in weights)
        sell_votes = sum(analyses[tf]["direction"] == "SELL" for tf in weights)
        pre_buy = sum(
            analyses[tf]["agents"]["Pre-Big Movement"]["direction"] == "PRE-BIG BUY"
            for tf in weights
        )
        pre_sell = sum(
            analyses[tf]["agents"]["Pre-Big Movement"]["direction"] == "PRE-BIG SELL"
            for tf in weights
        )
        if buy_votes >= 2 and weighted >= 5:
            final_signal = "BUY"
        elif sell_votes >= 2 and weighted <= -5:
            final_signal = "SELL"
        elif pre_buy >= 2 and weighted > 0:
            final_signal = "PRE-BIG BUY"
        elif pre_sell >= 2 and weighted < 0:
            final_signal = "PRE-BIG SELL"
        else:
            final_signal = "WAIT"
        # A consensus strength score, not a probability or win rate.
        confidence = min(100, round(abs(weighted) / 12 * 100))

    row = {
        "Symbol": symbol,
        "Yahoo Symbol": yahoo_symbol,
        "Final Signal": final_signal,
        "Consensus Score": round(weighted, 2),
        "Confirmation Strength %": confidence,
        "5M": analyses["5M"]["direction"],
        "15M": analyses["15M"]["direction"],
        "1H": analyses["1H"]["direction"],
        "5M Score": analyses["5M"]["score"],
        "15M Score": analyses["15M"]["score"],
        "1H Score": analyses["1H"]["score"],
        "Last Price": analyses["15M"]["close"],
        "15M RSI": analyses["15M"]["rsi"],
        "Errors": " | ".join(payload.get("errors", [])),
    }
    for tf in TIMEFRAMES:
        for agent_name in AGENT_NAMES:
            item = analyses[tf]["agents"][agent_name]
            safe_name = agent_name.replace(" ", "_").replace("/", "_")
            row[f"{tf} {safe_name}"] = item["direction"]
            row[f"{tf} {safe_name} Score"] = item["score"]
            row[f"{tf} {safe_name} Reason"] = item["reason"]

    # Human-readable summary of the 15M agent reasons.
    row["15M Agent Reasons"] = " | ".join(
        f"{name}: {analyses['15M']['agents'][name]['reason']}"
        for name in AGENT_NAMES
    )
    return row


def show_ai_multi_agent_scanner(client=None):
    """Streamlit page; client retained for app.py compatibility but unused."""
    st.title("🤖 AI Multi-Agent Scanner — Batch Optimized")
    st.caption("Yahoo Finance • 12 chart/technical agents × 5M / 15M / 1H + separate News Intelligence • Batch download")
    st.info(
        "Symbol universe comes from scanner.py. Yahoo Finance supplies candles. "
        "FYERS history/quotes are not called by this module."
    )
    st.warning(
        "Yahoo Finance is an unofficial source. Intraday data can be delayed, missing, "
        "or rate-limited. Confirmation Strength is a technical consensus score, not "
        "a probability or guaranteed win rate."
    )

    if yf is None:
        st.error("Missing dependency: add yfinance>=0.2.50 to requirements.txt and redeploy.")
        return

    try:
        from scanner import load_nse_equity_symbols, load_fo_stocks
    except Exception as exc:
        st.error(f"Could not import symbol loaders from scanner.py: {exc}")
        st.info(
            "scanner.py must provide load_nse_equity_symbols() and load_fo_stocks(). "
            "This module does not import FYERS candle-data functions."
        )
        return

    c1, c2, c3 = st.columns(3)
    with c1:
        universe = st.selectbox("Universe", ["F&O Stocks", "NSE Equity"], key="ma_universe")
    with c2:
        batch_size = st.selectbox("Batch size", [25, 40, 50, 75, 100], index=2, key="ma_batch_size")
    with c3:
        st.metric("Analysis model", "12 Agents × 3 TF")

    try:
        if universe == "F&O Stocks":
            symbols = _normalise_symbols(load_fo_stocks())
        else:
            symbols = _normalise_symbols(load_nse_equity_symbols())
    except Exception as exc:
        st.error(f"Symbol list error: {exc}")
        return

    symbols = list(dict.fromkeys(s for s in symbols if not _is_index_symbol(s)))
    if not symbols:
        st.error("No stock symbols returned by scanner.py.")
        return

    st.caption(f"Available symbols: {len(symbols)}. Index symbols are excluded.")
    st.caption("Full Scan selects every symbol returned by the chosen universe loader.")

    period = st.selectbox("Yahoo history", ["60d"], index=0, key="ma_period")
    if st.button("🚀 RUN FULL BATCH SCAN", type="primary", use_container_width=True, key="run_multi_agent"):
        symbol_items = [(s, _to_yahoo_symbol(s)) for s in symbols]
        # Avoid duplicate Yahoo tickers while retaining the first original symbol.
        seen = set()
        unique_items = []
        for original, ticker in symbol_items:
            if ticker not in seen:
                seen.add(ticker)
                unique_items.append((original, ticker))

        progress = st.progress(0)
        status = st.empty()
        status.write(f"Preparing {len(unique_items)} symbols...")
        result_payload = {}
        batches = [unique_items[i:i + batch_size] for i in range(0, len(unique_items), batch_size)]
        total_batches = len(batches)

        def run_one_batch(batch):
            return _batch_download_and_analyse(batch, len(batch), period)

        all_rows = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            future_map = {
                executor.submit(run_one_batch, batch): (idx, batch)
                for idx, batch in enumerate(batches, start=1)
            }
            done = 0
            for future in concurrent.futures.as_completed(future_map):
                idx, batch = future_map[future]
                try:
                    payload, _ = future.result()
                    for original, ticker in batch:
                        item = payload.get(original, {
                            "frames": {tf: pd.DataFrame() for tf in TIMEFRAMES},
                            "errors": ["No batch result returned"],
                        })
                        all_rows.append(_make_result_row(original, ticker, item))
                except Exception as exc:
                    for original, ticker in batch:
                        all_rows.append(_make_result_row(
                            original, ticker,
                            {"frames": {tf: pd.DataFrame() for tf in TIMEFRAMES},
                             "errors": [f"Batch failed: {exc}"]}
                        ))
                done += 1
                progress.progress(done / max(total_batches, 1))
                status.write(f"Completed batch {done}/{total_batches} ({len(batch)} symbols)")

        result = pd.DataFrame(all_rows)

        # News requests are intentionally limited to technical candidates to avoid
        # thousands of individual Yahoo calls and rate limiting.
        news_limit = st.selectbox("News analysis limit (technical candidates)", [25, 50, 100, 200], index=2, key="ma_news_limit")
        news_enabled = st.checkbox("Enable News Intelligence agent (shortlisted stocks only)", value=True, key="ma_news_enabled")
        if news_enabled and not result.empty:
            candidates = result[result["Final Signal"].isin(
                ["BUY", "SELL", "PRE-BIG BUY", "PRE-BIG SELL"]
            )].head(news_limit)
            news_by_symbol = {}
            news_bar = st.progress(0)
            for n, (_, candidate) in enumerate(candidates.iterrows(), start=1):
                news_by_symbol[candidate["Symbol"]] = _news_agent(candidate["Yahoo Symbol"])
                news_bar.progress(n / max(len(candidates), 1))
            result["News Agent"] = result["Symbol"].map(
                lambda sym: news_by_symbol.get(sym, {
                    "direction": "NOT CHECKED", "score": 0,
                    "reason": "Not in technical shortlist / news limit"
                })["direction"]
            )
            result["News Score"] = result["Symbol"].map(
                lambda sym: news_by_symbol.get(sym, {"score": 0})["score"]
            )
            result["News Reason"] = result["Symbol"].map(
                lambda sym: news_by_symbol.get(sym, {
                    "reason": "Not in technical shortlist / news limit"
                })["reason"]
            )
        elif not result.empty:
            result["News Agent"] = "DISABLED"
            result["News Score"] = 0
            result["News Reason"] = "News analysis disabled"

        if not result.empty:
            result = result.sort_values(
                ["Confirmation Strength %", "Symbol"],
                ascending=[False, True],
            ).reset_index(drop=True)
        st.session_state["multi_agent_results"] = result
        st.session_state["multi_agent_scan_info"] = {
            "universe": universe,
            "symbols": len(unique_items),
            "batches": total_batches,
            "batch_size": batch_size,
            "completed_at": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST"),
        }
        status.success(f"Full scan completed: {len(result)} / {len(unique_items)} symbols processed.")

    result = st.session_state.get("multi_agent_results")
    if isinstance(result, pd.DataFrame) and not result.empty:
        info = st.session_state.get("multi_agent_scan_info", {})
        st.caption(
            f"Last scan: {info.get('universe', '')} • {info.get('symbols', len(result))} symbols • "
            f"{info.get('batches', '')} batches • {info.get('completed_at', '')}"
        )

        counts = result["Final Signal"].value_counts()
        cols = st.columns(5)
        for col, label in zip(cols, ["BUY", "SELL", "PRE-BIG BUY", "PRE-BIG SELL", "WAIT"]):
            col.metric(label, int(counts.get(label, 0)))
        col_no = st.columns(1)[0]
        col_no.metric("NO DATA", int(counts.get("NO DATA", 0)))

        filter_signal = st.selectbox(
            "Show signals",
            ["ALL", "BUY", "SELL", "PRE-BIG BUY", "PRE-BIG SELL", "WAIT", "NO DATA"],
            key="ma_filter",
        )
        view = result if filter_signal == "ALL" else result[result["Final Signal"] == filter_signal]
        st.dataframe(view, use_container_width=True, hide_index=True)

        csv_data = result.to_csv(index=False).encode("utf-8-sig")
        st.download_button(
            "⬇️ Download Full CSV",
            csv_data,
            file_name=f"multi_agent_full_scan_{datetime.now(IST):%Y%m%d_%H%M%S}.csv",
            mime="text/csv",
            key="ma_csv",
        )

        try:
            buffer = io.BytesIO()
            with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
                result.to_excel(writer, index=False, sheet_name="AI Scanner")
            st.download_button(
                "⬇️ Download Full Excel",
                buffer.getvalue(),
                file_name=f"multi_agent_full_scan_{datetime.now(IST):%Y%m%d_%H%M%S}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key="ma_xlsx",
            )
        except Exception as exc:
            st.info(f"Excel export unavailable: {exc}")

        incomplete = result[result["Final Signal"] == "NO DATA"]
        if not incomplete.empty:
            with st.expander(f"Yahoo data missing / insufficient ({len(incomplete)})"):
                st.dataframe(
                    incomplete[["Symbol", "Yahoo Symbol", "Errors"]],
                    use_container_width=True,
                    hide_index=True,
                )

        with st.expander("12 Chart/Technical Agents + News Intelligence — explanation"):
            st.write(
                "12 OHLCV-based chart/technical agents run on each timeframe (5M, 15M, 1H), "
                "giving up to 36 timeframe-agent outputs per stock. News Intelligence is a "
                "separate headline-context check on shortlisted technical signals only. "
                "It is not run for every stock because individual news calls can be slow/rate-limited. "
                "Chart Pattern uses candle OHLCV data; it does not read chart screenshots. "
                "News sentiment is a basic keyword heuristic, not an AI language-model assessment. "
                "No news is treated as neutral, not as BUY or SELL."
            )
            st.dataframe(
                pd.DataFrame({"Agent": AGENT_NAMES}),
                use_container_width=True,
                hide_index=True,
            )
    elif isinstance(result, pd.DataFrame) and result.empty:
        st.info("No scan results yet. Press RUN FULL BATCH SCAN to start.")
