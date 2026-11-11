"""
AI Multi-Agent Scanner for NSE AI PRO V17
Yahoo Finance batch edition — revised.

- scanner.py only supplies the NSE / F&O symbol universe; this module combines both loaders without modifying scanner.py.
- Yahoo Finance supplies all OHLCV candles and technical-analysis data.
- FYERS is used ONLY for 5-level Market Depth on shortlisted BUY/SELL/PRE-BIG stocks.
- 17 OHLCV rule-based agents on completed 5M, 15M and 1H candles.
- Separate 20-candle 1M compression / volume buildup / breakout detector.
- Optional Yahoo headline context for the strongest technical shortlist.
- Compatible entry point: show_ai_multi_agent_scanner(client=None)

requirements.txt:
    yfinance>=0.2.50
    openpyxl>=3.1.0
"""

import io
import math
import re
import sqlite3
import threading
import time
from pathlib import Path
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
SESSION_OPEN = pd.Timedelta(hours=9, minutes=15)
SESSION_CLOSE = pd.Timedelta(hours=15, minutes=30)

INDEX_NAMES = {
    "NIFTY", "NIFTY50", "BANKNIFTY", "NIFTYBANK", "FINNIFTY",
    "MIDCPNIFTY", "NIFTYNEXT50", "SENSEX", "BANKEX",
}
TIMEFRAMES = {"5M": ("5m", 5), "15M": ("15m", 15), "1H": ("60m", 60)}
EARLY_TIMEFRAME = ("1m", 1)  # separate 1-minute feed; does not alter the 17-agent timeframes
AGENT_NAMES = [
    "Accumulation", "Distribution", "Liquidity Sweep", "Volume Expansion",
    "VWAP", "EMA Trend", "RSI Momentum", "Fair Value Gap",
    "HH/HL Structure", "LH/LL Structure", "Pre-Big Movement", "Chart Pattern",
    "Volume Analysis", "SMC Structure", "Order Block",
    "Buy/Sell Pressure", "Momentum Divergence",
]
SIGNAL_PRIORITY = {
    "BUY": 0, "SELL": 0, "PRE-BIG BUY": 1, "PRE-BIG SELL": 1,
    "WAIT": 2, "NO DATA": 3,
}
_YF_LOCK = threading.Lock()
FYERS_DEPTH_CACHE_TTL = 20       # seconds; prevents repeated depth calls on Streamlit reruns
FYERS_DEPTH_CALL_GAP = 1.05      # conservative one-call-per-second pacing
FYERS_DEPTH_DEFAULT_LIMIT = 20  # configurable shortlist cap per scan



# SQLite candle memory: persists completed Yahoo candles between Streamlit reruns.
# On hosted ephemeral filesystems, use a persistent mounted disk for survival across redeploys.
MEMORY_DB_PATH = Path(__file__).with_name("ai_multi_agent_candle_memory.sqlite3")

def _memory_connect():
    conn = sqlite3.connect(str(MEMORY_DB_PATH), timeout=20)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS candles (
        symbol TEXT NOT NULL, timeframe TEXT NOT NULL, candle_time TEXT NOT NULL,
        open REAL, high REAL, low REAL, close REAL, volume REAL, saved_at TEXT,
        PRIMARY KEY(symbol, timeframe, candle_time))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS scan_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT, scanned_at TEXT NOT NULL,
        symbol TEXT NOT NULL, final_signal TEXT, direction_movement TEXT,
        last_price REAL, yahoo_signal_time TEXT)""")
    return conn

def _save_candle_memory(batch, per_tf):
    now_text = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S %Z")
    records = []
    for original, ticker in batch:
        for tf, frame_map in per_tf.items():
            frame = frame_map.get(ticker)
            if frame is None or frame.empty:
                continue
            for r in frame[["Time", "Open", "High", "Low", "Close", "Volume"]].itertuples(index=False, name=None):
                ts = pd.Timestamp(r[0])
                if ts.tzinfo is None:
                    ts = ts.tz_localize(IST)
                else:
                    ts = ts.tz_convert(IST)
                records.append((str(original), tf, ts.isoformat(),
                                float(r[1]), float(r[2]), float(r[3]),
                                float(r[4]), float(r[5]), now_text))
    if not records:
        return 0
    with _memory_connect() as conn:
        conn.executemany("""INSERT INTO candles
            (symbol,timeframe,candle_time,open,high,low,close,volume,saved_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(symbol,timeframe,candle_time) DO UPDATE SET
            open=excluded.open, high=excluded.high, low=excluded.low,
            close=excluded.close, volume=excluded.volume, saved_at=excluded.saved_at""", records)
    return len(records)

def _load_candle_memory(symbol, timeframe):
    """Load previously saved candles for one source symbol/timeframe."""
    try:
        with _memory_connect() as conn:
            rows = conn.execute(
                """SELECT candle_time, open, high, low, close, volume
                   FROM candles WHERE symbol=? AND timeframe=? ORDER BY candle_time""",
                (str(symbol), str(timeframe)),
            ).fetchall()
        if not rows:
            return pd.DataFrame(columns=["Time", "Open", "High", "Low", "Close", "Volume"])
        frame = pd.DataFrame(rows, columns=["Time", "Open", "High", "Low", "Close", "Volume"])
        frame["Time"] = pd.to_datetime(frame["Time"], errors="coerce", utc=True).dt.tz_convert(IST)
        for col in ("Open", "High", "Low", "Close", "Volume"):
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
        frame = frame.dropna(subset=["Time", "Open", "High", "Low", "Close", "Volume"])
        return frame.sort_values("Time").drop_duplicates("Time", keep="last").reset_index(drop=True)
    except Exception:
        return pd.DataFrame(columns=["Time", "Open", "High", "Low", "Close", "Volume"])

def _merge_candle_frames(cached, fresh):
    """Merge stored and fresh Yahoo candles; fresh overlapping bars take precedence."""
    parts = [x for x in (cached, fresh) if isinstance(x, pd.DataFrame) and not x.empty]
    if not parts:
        return pd.DataFrame(columns=["Time", "Open", "High", "Low", "Close", "Volume"])
    merged = pd.concat(parts, ignore_index=True)
    merged["Time"] = pd.to_datetime(merged["Time"], errors="coerce", utc=True).dt.tz_convert(IST)
    return (merged.dropna(subset=["Time"]).sort_values("Time")
            .drop_duplicates("Time", keep="last").reset_index(drop=True))

def _save_scan_history(result, scanned_at):
    if result is None or result.empty:
        return 0
    rows = [(scanned_at, str(r.get("Symbol", "")), str(r.get("Final Signal", "")),
             str(r.get("Direction Movement", "")),
             _safe_num(r.get("Last Price"), 0), str(r.get("Signal Time (IST)", "")))
            for _, r in result.iterrows()]
    with _memory_connect() as conn:
        conn.executemany("""INSERT INTO scan_history
            (scanned_at,symbol,final_signal,direction_movement,last_price,yahoo_signal_time)
            VALUES (?,?,?,?,?,?)""", rows)
    return len(rows)

def _memory_stats():
    try:
        with _memory_connect() as conn:
            candles = conn.execute("SELECT COUNT(*) FROM candles").fetchone()[0]
            scans = conn.execute("SELECT COUNT(*) FROM scan_history").fetchone()[0]
            symbols = conn.execute("SELECT COUNT(DISTINCT symbol) FROM candles").fetchone()[0]
        return candles, scans, symbols
    except Exception:
        return 0, 0, 0

def _is_market_hours(now=None):
    now = now or datetime.now(IST)
    return now.weekday() < 5 and (now.hour, now.minute) >= (9, 15) and (now.hour, now.minute) < (15, 30)

def _stretch():
    try:
        major, minor = (int(x) for x in st.__version__.split(".")[:2])
        if (major, minor) >= (1, 49):
            return {"width": "stretch"}
    except Exception:
        pass
    return {"use_container_width": True}


def _normalise_symbols(values):
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
    if isinstance(values, str):
        values = [values]
    result, seen = [], set()
    for item in values:
        if isinstance(item, dict):
            item = (item.get("symbol") or item.get("Symbol")
                    or item.get("fyers_symbol") or item.get("tradingsymbol"))
        if item:
            symbol = str(item).strip()
            if symbol and symbol not in seen:
                seen.add(symbol)
                result.append(symbol)
    return result


def _clean_base(symbol):
    value = str(symbol).strip().upper()
    for prefix in ("NSE:", "BSE:"):
        if value.startswith(prefix):
            value = value[len(prefix):]
    for suffix in ("-INDEX", "-EQ", "-BE", "_EQ"):
        if value.endswith(suffix):
            value = value[:-len(suffix)]
    return value


def _is_index_symbol(symbol):
    raw = str(symbol).upper()
    if raw.endswith("-INDEX"):
        return True
    return _clean_base(symbol).split("_")[0] in INDEX_NAMES


def _to_yahoo_symbol(symbol):
    value = _clean_base(symbol)
    if value.endswith((".NS", ".BO")):
        return value
    return f"{value}.NS"


def _flatten_single_ticker_columns(df):
    """Flatten columns only after ticker extraction; keep OHLCV labels."""
    out = df.copy()
    if isinstance(out.columns, pd.MultiIndex):
        # For a single ticker, select the level containing standard OHLCV names.
        wanted = {"open", "high", "low", "close", "volume"}
        chosen = None
        for level in range(out.columns.nlevels):
            vals = {str(x).strip().lower() for x in out.columns.get_level_values(level)}
            if len(wanted.intersection(vals)) >= 4:
                chosen = level
                break
        if chosen is None:
            return pd.DataFrame()
        out.columns = out.columns.get_level_values(chosen)
    out.columns = [str(c).strip().capitalize() for c in out.columns]
    return out


def _closed_candles(df, minutes):
    """
    Clean one ticker's OHLCV data and retain only completed NSE-session bars.
    Regular bars must end by 15:30. A 60m bar beginning exactly at 15:15 is
    treated as the NSE final 15-minute partial bar and ends at 15:30.
    """
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()

    out = _flatten_single_ticker_columns(df)
    if out.empty:
        return pd.DataFrame()
    if "Time" not in out.columns:
        out["Time"] = out.index

    out["Time"] = pd.to_datetime(out["Time"], errors="coerce")
    out = out.dropna(subset=["Time"]).copy()
    if out.empty:
        return pd.DataFrame()

    try:
        if out["Time"].dt.tz is None:
            out["Time"] = out["Time"].dt.tz_localize(
                IST, ambiguous="NaT", nonexistent="shift_forward"
            )
        else:
            out["Time"] = out["Time"].dt.tz_convert(IST)
    except (TypeError, AttributeError, ValueError):
        return pd.DataFrame()

    out = out.dropna(subset=["Time"]).sort_values("Time")
    out = out.drop_duplicates(subset=["Time"], keep="last").copy()

    required = ["Open", "High", "Low", "Close", "Volume"]
    if any(c not in out.columns for c in required):
        return pd.DataFrame()
    for col in required:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=required).copy()

    # Reject malformed candles, zero/negative prices and negative volume.
    max_oc = out[["Open", "Close"]].max(axis=1)
    min_oc = out[["Open", "Close"]].min(axis=1)
    valid_ohlc = (
        (out["Open"] > 0) & (out["High"] > 0) &
        (out["Low"] > 0) & (out["Close"] > 0) &
        (out["High"] >= max_oc) & (out["Low"] <= min_oc) &
        (out["High"] >= out["Low"]) & (out["Volume"] >= 0)
    )
    out = out.loc[valid_ohlc].copy()
    if out.empty:
        return pd.DataFrame()

    now = pd.Timestamp.now(tz=IST)
    tod = out["Time"] - out["Time"].dt.normalize()
    session_start = tod >= SESSION_OPEN
    session_before_close = tod < SESSION_CLOSE
    same_day_end = out["Time"] + pd.to_timedelta(minutes, unit="m")

    # Only the known final 60-minute interval is shortened to the NSE close.
    if minutes == 60:
        final_partial = tod == pd.Timedelta(hours=15, minutes=15)
        same_day_end = same_day_end.where(~final_partial, out["Time"].dt.normalize() + SESSION_CLOSE)

    session_end = out["Time"].dt.normalize() + SESSION_CLOSE
    valid_end = same_day_end <= session_end
    completed = same_day_end <= now

    out = out.loc[session_start & session_before_close & valid_end & completed].copy()
    return out.reset_index(drop=True)


@st.cache_data(ttl=60, show_spinner=False, max_entries=8)
def _download_yahoo_batch_cached(tickers_tuple, interval, period):
    if yf is None:
        raise RuntimeError("yfinance is not installed. Add yfinance>=0.2.50.")
    with _YF_LOCK:
        data = yf.download(
            tickers=list(tickers_tuple), period=period, interval=interval,
            auto_adjust=False, actions=False, progress=False, threads=False,
            group_by="ticker", timeout=25,
        )
    if data is None or data.empty:
        raise RuntimeError(f"Yahoo returned no data for {interval}.")
    return data


def _extract_one_ticker(batch_df, ticker):
    if batch_df is None or batch_df.empty:
        return pd.DataFrame()
    if not isinstance(batch_df.columns, pd.MultiIndex):
        return batch_df.copy()
    target = ticker.upper()
    for level in range(batch_df.columns.nlevels):
        values = batch_df.columns.get_level_values(level).unique()
        for value in values:
            if str(value).upper() == target:
                try:
                    part = batch_df.xs(value, axis=1, level=level, drop_level=True)
                    return _flatten_single_ticker_columns(part)
                except Exception:
                    continue
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
    value = 100 - (100 / (1 + rs))
    value = value.where(~((loss == 0) & (gain > 0)), 100.0)
    return value.where(~((loss == 0) & (gain == 0)), 50.0)


def _vwap_last_session(df):
    if df.empty:
        return np.nan
    x = df.copy()
    x["_date"] = x["Time"].dt.date
    x = x[x["_date"] == x["_date"].iloc[-1]]
    typical = (x["High"] + x["Low"] + x["Close"]) / 3
    volume = x["Volume"].clip(lower=0)
    total = volume.sum()
    if total <= 0:
        return np.nan
    return _safe_num((typical * volume).sum() / total, np.nan)


def _agent_result(direction="WAIT", score=0, reason="No clear setup"):
    return {"direction": direction, "score": int(score), "reason": reason}


def _run_17_agents(df):
    """17 deterministic OHLCV rule engines; scores are not probabilities."""
    if df is None or len(df) < 30:
        return {n: _agent_result("NO DATA", 0, "Need 30 completed candles")
                for n in AGENT_NAMES}

    d = df.copy().reset_index(drop=True)
    o, h, l, c, v = (d[k].astype(float) for k in
                     ("Open", "High", "Low", "Close", "Volume"))
    last = _safe_num(c.iloc[-1])
    if last <= 0:
        return {n: _agent_result("NO DATA", 0, "Invalid last close") for n in AGENT_NAMES}

    ema9, ema21, ema50 = (c.ewm(span=p, adjust=False).mean() for p in (9, 21, 50))
    rsi = _rsi(c)
    avg_vol = _safe_num(v.shift(1).rolling(20, min_periods=10).mean().iloc[-1])
    rvol = _safe_num(v.iloc[-1] / avg_vol) if avg_vol > 0 else 0.0
    result = {}

    recent = d.iloc[-10:]
    range_pct = (_safe_num(recent["High"].max()) - _safe_num(recent["Low"].min())) / last * 100
    span = max(_safe_num(h.iloc[-1] - l.iloc[-1]), 1e-9)
    close_loc = (last - _safe_num(l.iloc[-1])) / span

    result["Accumulation"] = (
        _agent_result("BUY", 1, "Tight range; close near candle high")
        if range_pct <= 3 and close_loc >= .65 and last >= _safe_num(c.iloc[-5])
        else _agent_result()
    )
    result["Distribution"] = (
        _agent_result("SELL", -1, "Tight range; close near candle low")
        if range_pct <= 3 and close_loc <= .35 and last <= _safe_num(c.iloc[-5])
        else _agent_result()
    )

    prior_high, prior_low = _safe_num(h.iloc[-11:-1].max()), _safe_num(l.iloc[-11:-1].min())
    if _safe_num(h.iloc[-1]) > prior_high and last < prior_high:
        result["Liquidity Sweep"] = _agent_result("SELL", -2, "Swept prior highs; closed back below")
    elif _safe_num(l.iloc[-1]) < prior_low and last > prior_low:
        result["Liquidity Sweep"] = _agent_result("BUY", 2, "Swept prior lows; closed back above")
    else:
        result["Liquidity Sweep"] = _agent_result("WAIT", 0, "No confirmed 10-candle sweep")

    candle_change = _safe_num(c.iloc[-1] - o.iloc[-1])
    if rvol >= 1.8 and candle_change > 0:
        result["Volume Expansion"] = _agent_result("BUY", 2, f"RVOL {rvol:.2f}x; bullish candle")
    elif rvol >= 1.8 and candle_change < 0:
        result["Volume Expansion"] = _agent_result("SELL", -2, f"RVOL {rvol:.2f}x; bearish candle")
    else:
        result["Volume Expansion"] = _agent_result("WAIT", 0, f"RVOL {rvol:.2f}x; no directional expansion")

    vwap = _vwap_last_session(d)
    if pd.notna(vwap) and last > vwap * 1.001:
        result["VWAP"] = _agent_result("BUY", 1, f"Above session VWAP ({vwap:.2f})")
    elif pd.notna(vwap) and last < vwap * .999:
        result["VWAP"] = _agent_result("SELL", -1, f"Below session VWAP ({vwap:.2f})")
    else:
        result["VWAP"] = _agent_result("WAIT", 0, "Near VWAP or volume unavailable")

    if ema9.iloc[-1] > ema21.iloc[-1] and last > ema50.iloc[-1]:
        result["EMA Trend"] = _agent_result("BUY", 2, "EMA9 > EMA21; price > EMA50")
    elif ema9.iloc[-1] < ema21.iloc[-1] and last < ema50.iloc[-1]:
        result["EMA Trend"] = _agent_result("SELL", -2, "EMA9 < EMA21; price < EMA50")
    else:
        result["EMA Trend"] = _agent_result("WAIT", 0, "EMA trend mixed")

    rsi_now = _safe_num(rsi.iloc[-1], 50)
    result["RSI Momentum"] = (
        _agent_result("BUY", 1, f"RSI bullish ({rsi_now:.1f})") if rsi_now >= 55 else
        _agent_result("SELL", -1, f"RSI bearish ({rsi_now:.1f})") if rsi_now <= 45 else
        _agent_result("WAIT", 0, f"RSI neutral ({rsi_now:.1f})")
    )

    if _safe_num(l.iloc[-1]) > _safe_num(h.iloc[-3]):
        result["Fair Value Gap"] = _agent_result("BUY", 1, "Bullish three-candle FVG")
    elif _safe_num(h.iloc[-1]) < _safe_num(l.iloc[-3]):
        result["Fair Value Gap"] = _agent_result("SELL", -1, "Bearish three-candle FVG")
    else:
        result["Fair Value Gap"] = _agent_result("WAIT", 0, "No fresh three-candle FVG")

    old, new = d.iloc[-10:-5], d.iloc[-5:]
    old_hi, new_hi = _safe_num(old["High"].max()), _safe_num(new["High"].max())
    old_lo, new_lo = _safe_num(old["Low"].min()), _safe_num(new["Low"].min())
    result["HH/HL Structure"] = (
        _agent_result("BUY", 2, "Recent window: higher high and higher low")
        if new_hi > old_hi and new_lo > old_lo else _agent_result("WAIT", 0, "HH/HL not confirmed")
    )
    result["LH/LL Structure"] = (
        _agent_result("SELL", -2, "Recent window: lower high and lower low")
        if new_hi < old_hi and new_lo < old_lo else _agent_result("WAIT", 0, "LH/LL not confirmed")
    )

    recent_range = _safe_num(h.iloc[-8:].max() - l.iloc[-8:].min())
    prior_range = _safe_num(h.iloc[-20:-8].max() - l.iloc[-20:-8].min())
    contraction = prior_range > 0 and recent_range < prior_range * .70
    near_high = last >= _safe_num(h.iloc[-8:].max()) * .995
    near_low = last <= _safe_num(l.iloc[-8:].min()) * 1.005
    if contraction and rvol >= 1.2 and near_high:
        result["Pre-Big Movement"] = _agent_result("PRE-BIG BUY", 2, "Range contraction + RVOL; near range high")
    elif contraction and rvol >= 1.2 and near_low:
        result["Pre-Big Movement"] = _agent_result("PRE-BIG SELL", -2, "Range contraction + RVOL; near range low")
    else:
        result["Pre-Big Movement"] = _agent_result("WAIT", 0, "Pre-movement conditions not aligned")

    prev_o, prev_c = _safe_num(o.iloc[-2]), _safe_num(c.iloc[-2])
    last_o, last_c = _safe_num(o.iloc[-1]), _safe_num(c.iloc[-1])
    last_h, last_l = _safe_num(h.iloc[-1]), _safe_num(l.iloc[-1])
    body = abs(last_c - last_o)
    candle_range = max(last_h - last_l, 1e-9)
    upper_wick = last_h - max(last_o, last_c)
    lower_wick = min(last_o, last_c) - last_l
    bullish_engulf = prev_c < prev_o and last_c > last_o and last_o <= prev_c and last_c >= prev_o
    bearish_engulf = prev_c > prev_o and last_c < last_o and last_o >= prev_c and last_c <= prev_o
    hammer = lower_wick >= max(body * 2, candle_range * .45) and upper_wick <= candle_range * .25
    star = upper_wick >= max(body * 2, candle_range * .45) and lower_wick <= candle_range * .25
    resistance, support = _safe_num(h.iloc[-21:-1].max()), _safe_num(l.iloc[-21:-1].min())
    if bullish_engulf or (hammer and last_c > last_o):
        result["Chart Pattern"] = _agent_result("BUY", 2, "Bullish engulfing / hammer")
    elif bearish_engulf or (star and last_c < last_o):
        result["Chart Pattern"] = _agent_result("SELL", -2, "Bearish engulfing / shooting star")
    elif last_c > resistance and rvol >= 1.2:
        result["Chart Pattern"] = _agent_result("BUY", 2, "20-candle resistance break with volume")
    elif last_c < support and rvol >= 1.2:
        result["Chart Pattern"] = _agent_result("SELL", -2, "20-candle support break with volume")
    else:
        result["Chart Pattern"] = _agent_result("WAIT", 0, "No confirmed pattern / volume-backed break")

    # Five additional completed-candle agents.
    # 13) Volume Analysis: relative volume and directional volume balance.
    vol_avg = v.shift(1).rolling(20, min_periods=10).mean()
    vol_rvol = _safe_num(v.iloc[-1] / vol_avg.iloc[-1]) if pd.notna(vol_avg.iloc[-1]) and vol_avg.iloc[-1] > 0 else 0.0
    up_volume = _safe_num(v.iloc[-5:][c.iloc[-5:] >= o.iloc[-5:]].sum())
    down_volume = _safe_num(v.iloc[-5:][c.iloc[-5:] < o.iloc[-5:]].sum())
    volume_total = up_volume + down_volume
    volume_balance = (up_volume - down_volume) / volume_total if volume_total > 0 else 0.0
    if vol_rvol >= 1.5 and volume_balance >= 0.20:
        result["Volume Analysis"] = _agent_result("BUY", 2, f"Positive volume balance; RVOL {vol_rvol:.2f}x")
    elif vol_rvol >= 1.5 and volume_balance <= -0.20:
        result["Volume Analysis"] = _agent_result("SELL", -2, f"Negative volume balance; RVOL {vol_rvol:.2f}x")
    else:
        result["Volume Analysis"] = _agent_result("WAIT", 0, f"Volume not directional; RVOL {vol_rvol:.2f}x")

    # 14) SMC: completed close beyond the previous 10-candle structure.
    smc_high = _safe_num(h.iloc[-11:-1].max())
    smc_low = _safe_num(l.iloc[-11:-1].min())
    prior_trend_up = _safe_num(c.iloc[-6]) > _safe_num(c.iloc[-11])
    prior_trend_down = _safe_num(c.iloc[-6]) < _safe_num(c.iloc[-11])
    if last > smc_high:
        label = "Bullish CHoCH/BOS" if prior_trend_down else "Bullish BOS"
        result["SMC Structure"] = _agent_result("BUY", 2, f"{label}: close above prior structure")
    elif last < smc_low:
        label = "Bearish CHoCH/BOS" if prior_trend_up else "Bearish BOS"
        result["SMC Structure"] = _agent_result("SELL", -2, f"{label}: close below prior structure")
    else:
        result["SMC Structure"] = _agent_result("WAIT", 0, "No completed structure break")

    # 15) Order Block: identify a recent opposing candle and check retest.
    ob_window = d.iloc[-11:-1]
    bullish_ob = next(
        ((float(row.Low), float(row.High)) for row in ob_window.iloc[::-1].itertuples()
         if row.Close < row.Open), None
    )
    bearish_ob = next(
        ((float(row.Low), float(row.High)) for row in ob_window.iloc[::-1].itertuples()
         if row.Close > row.Open), None
    )
    bullish_retest = bullish_ob is not None and bullish_ob[0] <= last <= bullish_ob[1] * 1.003
    bearish_retest = bearish_ob is not None and bearish_ob[0] * 0.997 <= last <= bearish_ob[1]
    if bullish_retest and last >= _safe_num(o.iloc[-1]):
        result["Order Block"] = _agent_result("BUY", 2, f"Bullish OB retest {bullish_ob[0]:.2f}-{bullish_ob[1]:.2f}")
    elif bearish_retest and last <= _safe_num(o.iloc[-1]):
        result["Order Block"] = _agent_result("SELL", -2, f"Bearish OB retest {bearish_ob[0]:.2f}-{bearish_ob[1]:.2f}")
    else:
        result["Order Block"] = _agent_result("WAIT", 0, "No confirmed order-block retest")

    # 16) Buy/Sell Pressure: candle close location plus directional volume.
    pressure_close = (last - _safe_num(l.iloc[-1])) / span
    pressure_score = (pressure_close - 0.5) * 2 + volume_balance
    if pressure_score >= 0.65:
        result["Buy/Sell Pressure"] = _agent_result("BUY", 2, f"Buying pressure dominant ({pressure_score:.2f})")
    elif pressure_score <= -0.65:
        result["Buy/Sell Pressure"] = _agent_result("SELL", -2, f"Selling pressure dominant ({pressure_score:.2f})")
    else:
        result["Buy/Sell Pressure"] = _agent_result("WAIT", 0, f"Pressure balanced ({pressure_score:.2f})")

    # 17) Momentum Divergence: compare recent price extremes with RSI windows.
    rsi_series = _rsi(c)
    old_price_low = _safe_num(l.iloc[-10:-5].min())
    new_price_low = _safe_num(l.iloc[-5:].min())
    old_price_high = _safe_num(h.iloc[-10:-5].max())
    new_price_high = _safe_num(h.iloc[-5:].max())
    old_rsi = _safe_num(rsi_series.iloc[-10:-5].mean(), 50.0)
    new_rsi = _safe_num(rsi_series.iloc[-5:].mean(), 50.0)
    if new_price_low < old_price_low and new_rsi > old_rsi + 2:
        result["Momentum Divergence"] = _agent_result("BUY", 2, "Possible bullish RSI divergence")
    elif new_price_high > old_price_high and new_rsi < old_rsi - 2:
        result["Momentum Divergence"] = _agent_result("SELL", -2, "Possible bearish RSI divergence")
    else:
        result["Momentum Divergence"] = _agent_result("WAIT", 0, "No clear regular RSI divergence")

    return result


# News is contextual headline keyword scoring, not an AI model or standalone signal.
_POSITIVE = [r"profits?", r"growth", r"upgrades?", r"upgraded", r"order wins?", r"approval",
             r"records?", r"buybacks?", r"dividends?", r"partnerships?", r"strong results",
             r"beats? estimates"]
_NEGATIVE = [r"loss(?:es)?", r"downgrades?", r"downgraded", r"fraud", r"default(?:s|ed)?",
             r"probe", r"bans?", r"banned", r"penalty", r"penalties", r"resignation",
             r"weak results", r"misses? estimates", r"debt"]
_POS_RE = [re.compile(r"\b" + p + r"\b") for p in _POSITIVE]
_NEG_RE = [re.compile(r"\b" + p + r"\b") for p in _NEGATIVE]


@st.cache_data(ttl=300, show_spinner=False)
def _fetch_yahoo_news_cached(yahoo_symbol):
    if yf is None:
        return []
    try:
        with _YF_LOCK:
            raw = yf.Ticker(yahoo_symbol).news or []
        cleaned = []
        for item in raw[:8]:
            if not isinstance(item, dict):
                continue
            content = item.get("content", item)
            if not isinstance(content, dict):
                content = item
            title = content.get("title") or item.get("title") or ""
            provider = content.get("provider")
            publisher = provider.get("displayName", "") if isinstance(provider, dict) else content.get("publisher", "")
            pub = content.get("pubDate") or item.get("providerPublishTime")
            try:
                published = pd.Timestamp(pub) if isinstance(pub, str) else (
                    pd.to_datetime(pub, unit="s", errors="coerce") if pub else pd.NaT
                )
            except Exception:
                published = pd.NaT
            if title:
                cleaned.append({"title": str(title), "publisher": str(publisher), "published": published})
        return cleaned
    except Exception:
        return []


def _news_agent(yahoo_symbol):
    items = _fetch_yahoo_news_cached(yahoo_symbol)
    if not items:
        return {"direction": "NO NEWS", "score": 0, "reason": "No usable Yahoo headlines"}
    now = pd.Timestamp.now(tz="UTC")
    total, ages, titles = 0, [], []
    for item in items:
        headline = item["title"].lower()
        score = sum(bool(p.search(headline)) for p in _POS_RE) - sum(bool(p.search(headline)) for p in _NEG_RE)
        published = item.get("published")
        age = None
        if published is not None and not pd.isna(published):
            stamp = pd.Timestamp(published)
            stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
            age = max(0.0, (now - stamp).total_seconds() / 3600)
            ages.append(age)
        if age is not None and age > 48:
            score = 0
        total += score
        titles.append(item["title"][:110])
    direction = "POSITIVE NEWS" if total > 0 else "NEGATIVE NEWS" if total < 0 else "NEUTRAL NEWS"
    age_text = f"; newest item {min(ages):.1f}h old" if ages else ""
    return {"direction": direction, "score": 1 if total > 0 else -1 if total < 0 else 0,
            "reason": f"Keyword headline context{age_text}: " + " | ".join(titles[:3])}


def _analyse_timeframe(df):
    agents = _run_17_agents(df)
    score = int(sum(x["score"] for x in agents.values()))
    buy_votes = sum(x["direction"] in ("BUY", "PRE-BIG BUY") for x in agents.values())
    sell_votes = sum(x["direction"] in ("SELL", "PRE-BIG SELL") for x in agents.values())
    valid = sum(x["direction"] != "NO DATA" for x in agents.values())
    if valid < 8:
        direction = "NO DATA"
    elif score >= 5 and buy_votes >= 3:
        direction = "BUY"
    elif score <= -5 and sell_votes >= 3:
        direction = "SELL"
    else:
        direction = "WAIT"
    has_data = df is not None and not df.empty and "Close" in df.columns
    close = round(_safe_num(df["Close"].iloc[-1]), 2) if has_data else None
    rsi_value = round(_safe_num(_rsi(df["Close"].astype(float)).iloc[-1]), 2) if has_data and len(df) >= 15 else None
    return {"direction": direction, "score": score, "agents": agents,
            "close": close, "rsi": rsi_value, "candles": len(df) if has_data else 0}


def _early_20m_analysis(df):
    """20 completed 1-minute candles: compression, volume buildup, pressure and breakout."""
    empty = {
        "20M Status": "NO DATA", "20M Direction": "WAIT", "20M Pre-Move Score": 0,
        "20M Range High": None, "20M Range Low": None, "20M Range %": None,
        "20M RVOL": None, "20M Pressure %": None, "20M Last Close": None,
        "20M Reason": "Need at least 21 completed 1-minute candles",
    }
    if df is None or not isinstance(df, pd.DataFrame) or len(df) < 21:
        return empty
    d = df.sort_values("Time").drop_duplicates("Time", keep="last").tail(21).reset_index(drop=True)
    # The latest completed candle is the test candle; range boundaries use the previous 20.
    base = d.iloc[-21:-1]
    last = d.iloc[-1]
    high = _safe_num(base["High"].max())
    low = _safe_num(base["Low"].min())
    close = _safe_num(last["Close"])
    if low <= 0 or high <= low or close <= 0:
        return empty
    range_pct = (high - low) / close * 100.0
    previous = d.iloc[-21:-6]
    recent5 = d.iloc[-6:-1]
    prev_avg = _safe_num(previous["Volume"].mean())
    recent_avg = _safe_num(recent5["Volume"].mean())
    last_vol = _safe_num(last["Volume"])
    rvol = last_vol / max(_safe_num(base["Volume"].mean()), 1.0)
    buildup = recent_avg / max(prev_avg, 1.0)
    candle_span = max(_safe_num(last["High"] - last["Low"]), 1e-9)
    close_location = max(0.0, min(1.0, (close - _safe_num(last["Low"])) / candle_span))
    pressure = (close_location - 0.5) * 200.0
    breakout_up = close > high
    breakout_down = close < low
    # Compare 20-minute width to the preceding 20 completed candles when available.
    prior_compression = False
    if len(df) >= 41:
        prior = df.sort_values("Time").drop_duplicates("Time", keep="last").iloc[-41:-21]
        prior_width = _safe_num(prior["High"].max() - prior["Low"].min())
        current_width = high - low
        prior_compression = prior_width > 0 and current_width <= prior_width * 0.75
    else:
        # Without a prior 20-minute comparator, use a conservative absolute range cap.
        prior_compression = range_pct <= 1.0
    near_high = close >= high * 0.997
    near_low = close <= low * 1.003
    score = 0
    score += 25 if prior_compression else 0
    score += 20 if buildup >= 1.20 else 10 if buildup >= 1.05 else 0
    score += 20 if rvol >= 1.50 else 10 if rvol >= 1.20 else 0
    score += 20 if abs(pressure) >= 35 else 10 if abs(pressure) >= 15 else 0
    score += 15 if near_high or near_low else 0
    score = min(100, score)
    if breakout_up:
        status, direction = "CONFIRMED BREAKOUT", "UP"
        reason = "Latest completed 1M close broke above previous 20-candle high"
        score = max(score, 80)
    elif breakout_down:
        status, direction = "CONFIRMED BREAKOUT", "DOWN"
        reason = "Latest completed 1M close broke below previous 20-candle low"
        score = max(score, 80)
    elif prior_compression and buildup >= 1.05 and pressure >= 15 and near_high:
        status, direction = "WATCH", "UP"
        reason = "20M range compression + volume buildup + buying pressure near range high"
    elif prior_compression and buildup >= 1.05 and pressure <= -15 and near_low:
        status, direction = "WATCH", "DOWN"
        reason = "20M range compression + volume buildup + selling pressure near range low"
    else:
        status, direction = "WAIT", "WAIT"
        reason = "20M setup conditions are not aligned"
    return {
        "20M Status": status, "20M Direction": direction,
        "20M Pre-Move Score": int(score), "20M Range High": round(high, 2),
        "20M Range Low": round(low, 2), "20M Range %": round(range_pct, 3),
        "20M RVOL": round(rvol, 2), "20M Pressure %": round(pressure, 1),
        "20M Last Close": round(close, 2), "20M Reason": reason,
    }


def _make_result_row(symbol, yahoo_symbol, payload):
    frames = payload.get("frames", {})
    analyses = {tf: _analyse_timeframe(frames.get(tf, pd.DataFrame())) for tf in TIMEFRAMES}
    valid = all(analyses[tf]["direction"] != "NO DATA" for tf in TIMEFRAMES)

    weighted = 0.0
    if not valid:
        final_signal, strength = "NO DATA", 0
    else:
        weights = {"5M": .25, "15M": .45, "1H": .30}
        weighted = sum(analyses[tf]["score"] * weights[tf] for tf in weights)
        directions = [analyses[tf]["direction"] for tf in weights]
        buy_tf, sell_tf = directions.count("BUY"), directions.count("SELL")
        pre_buy = sum(analyses[tf]["agents"]["Pre-Big Movement"]["direction"] == "PRE-BIG BUY" for tf in weights)
        pre_sell = sum(analyses[tf]["agents"]["Pre-Big Movement"]["direction"] == "PRE-BIG SELL" for tf in weights)

        if buy_tf >= 2 and sell_tf == 0 and weighted >= 5:
            final_signal = "BUY"
        elif sell_tf >= 2 and buy_tf == 0 and weighted <= -5:
            final_signal = "SELL"
        # PRE-BIG is also blocked by any opposite timeframe trend.
        elif pre_buy >= 2 and sell_tf == 0 and weighted > 0:
            final_signal = "PRE-BIG BUY"
        elif pre_sell >= 2 and buy_tf == 0 and weighted < 0:
            final_signal = "PRE-BIG SELL"
        else:
            final_signal = "WAIT"
        strength = min(100, round(abs(weighted) / 12 * 100))

    # Signal timestamp is the close time of the latest completed 15M candle.
    # Entry/SL are populated only for actionable BUY/SELL signals.
    signal_time = None
    entry_price = None
    stop_loss = None
    atr_15m = None
    frame_15m = frames.get("15M", pd.DataFrame())
    if frame_15m is not None and not frame_15m.empty and "Time" in frame_15m.columns:
        try:
            last_bar_time = pd.Timestamp(frame_15m["Time"].iloc[-1])
            if last_bar_time.tzinfo is None:
                last_bar_time = last_bar_time.tz_localize(IST)
            else:
                last_bar_time = last_bar_time.tz_convert(IST)
            signal_time = (last_bar_time + pd.Timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S IST")
        except Exception:
            signal_time = None
        try:
            high = frame_15m["High"].astype(float)
            low = frame_15m["Low"].astype(float)
            close = frame_15m["Close"].astype(float)
            previous_close = close.shift(1)
            true_range = pd.concat(
                [(high - low), (high - previous_close).abs(), (low - previous_close).abs()],
                axis=1,
            ).max(axis=1)
            atr_value = true_range.rolling(14, min_periods=7).mean().iloc[-1]
            if pd.notna(atr_value) and float(atr_value) > 0:
                atr_15m = float(atr_value)
        except Exception:
            atr_15m = None

    if final_signal in ("BUY", "PRE-BIG BUY") and analyses["15M"]["close"] is not None and atr_15m:
        entry_price = float(analyses["15M"]["close"])
        stop_loss = entry_price - (1.5 * atr_15m)
    elif final_signal in ("SELL", "PRE-BIG SELL") and analyses["15M"]["close"] is not None and atr_15m:
        entry_price = float(analyses["15M"]["close"])
        stop_loss = entry_price + (1.5 * atr_15m)

    row = {
        "Symbol": symbol, "Yahoo Symbol": yahoo_symbol, "Final Signal": final_signal,
        "Signal Time (IST)": signal_time,
        "Entry Price": round(entry_price, 2) if entry_price is not None else None,
        "Stop Loss": round(stop_loss, 2) if stop_loss is not None else None,
        "15M ATR": round(atr_15m, 2) if atr_15m is not None else None,
        "Consensus Score": round(weighted, 2), "Confirmation Strength %": strength,
        "5M": analyses["5M"]["direction"], "15M": analyses["15M"]["direction"],
        "1H": analyses["1H"]["direction"], "5M Score": analyses["5M"]["score"],
        "15M Score": analyses["15M"]["score"], "1H Score": analyses["1H"]["score"],
        "5M Candles": analyses["5M"]["candles"], "15M Candles": analyses["15M"]["candles"],
        "1H Candles": analyses["1H"]["candles"], "Last Price": analyses["15M"]["close"],
        "15M RSI": analyses["15M"]["rsi"], "Errors": " | ".join(payload.get("errors", [])),
    }
    row.update(_early_20m_analysis(payload.get("early_1m", pd.DataFrame())))
    for tf in TIMEFRAMES:
        for name in AGENT_NAMES:
            item = analyses[tf]["agents"][name]
            safe = name.replace(" ", "_").replace("/", "_")
            row[f"{tf} {safe}"] = item["direction"]
            row[f"{tf} {safe} Score"] = item["score"]
            row[f"{tf} {safe} Reason"] = item["reason"]
    row["15M Agent Reasons"] = " | ".join(
        f"{n}: {analyses['15M']['agents'][n]['reason']}" for n in AGENT_NAMES
    )
    return row


def _no_data_row(symbol, ticker, message):
    return _make_result_row(symbol, ticker, {
        "frames": {tf: pd.DataFrame() for tf in TIMEFRAMES}, "errors": [message]
    })


def _fetch_interval(tickers, interval, minutes, period, retries=2):
    frames = {t: pd.DataFrame() for t in tickers}
    errors, pending = {}, list(tickers)
    for attempt in range(retries + 1):
        if not pending:
            break
        try:
            raw = _download_yahoo_batch_cached(tuple(pending), interval, period)
        except Exception as exc:
            for ticker in pending:
                errors[ticker] = str(exc)[:200]
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
            continue
        missing = []
        for ticker in pending:
            clean = _closed_candles(_extract_one_ticker(raw, ticker), minutes)
            if clean.empty:
                missing.append(ticker)
                errors[ticker] = "No completed valid NSE-session candles / ticker missing"
            else:
                frames[ticker] = clean
                errors.pop(ticker, None)
        if len(missing) == len(pending):
            break
        pending = missing
    return frames, errors


def _scan_batch(batch, period):
    tickers = [ticker for _, ticker in batch]
    per_tf, problems = {}, {ticker: [] for ticker in tickers}
    original_by_ticker = {ticker: original for original, ticker in batch}

    # If SQLite already has data for every ticker, fetch only a short overlap window.
    # First scan remains a full-history warm-up. The overlap refreshes late/adjusted bars.
    for label, (interval, minutes) in TIMEFRAMES.items():
        cached = {ticker: _load_candle_memory(original_by_ticker[ticker], label) for ticker in tickers}
        all_cached = all(not cached[t].empty for t in tickers)
        fetch_period = "2d" if all_cached else period
        fresh_frames, errors = _fetch_interval(tickers, interval, minutes, fetch_period)
        frames = {}
        for ticker in tickers:
            frames[ticker] = _merge_candle_frames(cached[ticker], fresh_frames.get(ticker))
            if frames[ticker].empty:
                problems[ticker].append(f"{label}: {errors.get(ticker, 'No cached or fresh data')}")
        per_tf[label] = frames

    # Dedicated 1-minute feed for the 20-candle early-movement detector.
    early_cached = {ticker: _load_candle_memory(original_by_ticker[ticker], "1M") for ticker in tickers}
    early_all_cached = all(not early_cached[t].empty for t in tickers)
    early_period = "2d" if early_all_cached else "7d"  # Yahoo 1m history is limited
    early_fresh, early_errors = _fetch_interval(tickers, "1m", 1, early_period)
    early_frames = {
        ticker: _merge_candle_frames(early_cached[ticker], early_fresh.get(ticker))
        for ticker in tickers
    }
    per_tf["1M"] = early_frames
    for ticker in tickers:
        if early_frames[ticker].empty:
            problems[ticker].append(f"1M: {early_errors.get(ticker, 'No 1M data')}")

    try:
        _save_candle_memory(batch, per_tf)
    except Exception:
        pass

    rows = []
    for original, ticker in batch:
        try:
            rows.append(_make_result_row(original, ticker, {
                "frames": {tf: per_tf[tf][ticker] for tf in TIMEFRAMES},
                "early_1m": per_tf["1M"][ticker],
                "errors": problems[ticker],
            }))
        except Exception as exc:
            rows.append(_no_data_row(original, ticker, f"Analysis error: {exc}"))
    return rows



def _to_fyers_equity_symbol(symbol):
    """Convert scanner universe symbols to FYERS NSE cash-market symbols."""
    raw = str(symbol or "").strip().upper()
    if raw.startswith("NSE:"):
        body = raw[4:]
    elif raw.startswith("BSE:"):
        return ""  # This scanner is configured for NSE equity/F&O universe.
    else:
        body = raw
    body = body.replace(".NS", "").replace(".BO", "")
    for suffix in ("-EQ", "-BE", "-INDEX", "_EQ"):
        if body.endswith(suffix):
            body = body[:-len(suffix)]
    if not body or body in INDEX_NAMES:
        return ""
    return f"NSE:{body}-EQ"


def _depth_levels(payload):
    """Return the FYERS symbol depth object across common SDK response wrappers."""
    if not isinstance(payload, dict):
        return None
    data = payload.get("d", payload.get("data", payload))
    if isinstance(data, dict):
        # FYERS normally keys d by the requested symbol.
        for value in data.values():
            if isinstance(value, dict) and any(k in value for k in ("bids", "ask", "asks")):
                return value
        if any(k in data for k in ("bids", "ask", "asks")):
            return data
    return None


def _normalise_depth_row(symbol, fyers_symbol, response, fetched_at=None):
    """Convert FYERS 5-level depth into transparent quantities and imbalance."""
    stamp = fetched_at or datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
    if not isinstance(response, dict) or str(response.get("s", "")).lower() not in ("ok", "success"):
        message = response.get("message", "FYERS depth response not successful") if isinstance(response, dict) else "Invalid FYERS response"
        return {"Symbol": symbol, "FYERS Symbol": fyers_symbol, "Depth Status": "DEPTH UNAVAILABLE",
                "Depth Signal": "N/A", "Best Bid": None, "Best Ask": None, "Spread": None,
                "Buy Qty (5L)": None, "Sell Qty (5L)": None, "Depth Imbalance %": None,
                "Depth Time (IST)": stamp, "Depth Message": str(message)[:180]}

    book = _depth_levels(response)
    if not book:
        return {"Symbol": symbol, "FYERS Symbol": fyers_symbol, "Depth Status": "DEPTH UNAVAILABLE",
                "Depth Signal": "N/A", "Best Bid": None, "Best Ask": None, "Spread": None,
                "Buy Qty (5L)": None, "Sell Qty (5L)": None, "Depth Imbalance %": None,
                "Depth Time (IST)": stamp, "Depth Message": "No bids/asks in FYERS response"}

    bids = book.get("bids") or book.get("buy") or []
    asks = book.get("ask") or book.get("asks") or book.get("sell") or []
    def level_price(level):
        return _safe_num(level.get("price"), np.nan) if isinstance(level, dict) else np.nan
    def level_qty(level):
        if not isinstance(level, dict):
            return 0.0
        return _safe_num(level.get("volume", level.get("qty", level.get("quantity", 0))))

    bids = list(bids)[:5] if isinstance(bids, (list, tuple)) else []
    asks = list(asks)[:5] if isinstance(asks, (list, tuple)) else []
    buy_qty = sum(level_qty(x) for x in bids)
    sell_qty = sum(level_qty(x) for x in asks)
    bid_prices = [level_price(x) for x in bids]
    ask_prices = [level_price(x) for x in asks]
    bid_prices = [x for x in bid_prices if np.isfinite(x) and x > 0]
    ask_prices = [x for x in ask_prices if np.isfinite(x) and x > 0]
    best_bid = max(bid_prices) if bid_prices else np.nan
    best_ask = min(ask_prices) if ask_prices else np.nan
    spread = best_ask - best_bid if np.isfinite(best_bid) and np.isfinite(best_ask) else np.nan
    total = buy_qty + sell_qty
    imbalance = ((buy_qty - sell_qty) / total * 100.0) if total > 0 else np.nan
    if not bids or not asks or total <= 0:
        depth_signal = "INSUFFICIENT DEPTH"
        status = "PARTIAL DEPTH"
    elif imbalance >= 15:
        depth_signal, status = "BUY-SIDE DOMINANT", "OK"
    elif imbalance <= -15:
        depth_signal, status = "SELL-SIDE DOMINANT", "OK"
    else:
        depth_signal, status = "BALANCED", "OK"

    return {
        "Symbol": symbol, "FYERS Symbol": fyers_symbol, "Depth Status": status,
        "Depth Signal": depth_signal,
        "Best Bid": round(best_bid, 2) if np.isfinite(best_bid) else None,
        "Best Ask": round(best_ask, 2) if np.isfinite(best_ask) else None,
        "Spread": round(spread, 2) if np.isfinite(spread) else None,
        "Buy Qty (5L)": int(buy_qty), "Sell Qty (5L)": int(sell_qty),
        "Depth Imbalance %": round(imbalance, 2) if np.isfinite(imbalance) else None,
        "Depth Time (IST)": stamp, "Depth Message": "5-level bid/ask quantities; snapshot only",
    }


def _fetch_fyers_depth_shortlist(client, shortlist, limit=FYERS_DEPTH_DEFAULT_LIMIT):
    """Fetch FYERS depth only for technical BUY/SELL/PRE-BIG shortlist rows.

    Cache is stored in Streamlit session_state. Calls are paced conservatively;
    missing client/API errors are reported as unavailable, never fabricated.
    """
    columns = ["Symbol", "FYERS Symbol", "Depth Status", "Depth Signal", "Best Bid", "Best Ask",
               "Spread", "Buy Qty (5L)", "Sell Qty (5L)", "Depth Imbalance %", "Depth Time (IST)", "Depth Message"]
    if shortlist is None or shortlist.empty:
        return pd.DataFrame(columns=columns)

    cache = st.session_state.setdefault("ma_fyers_depth_cache", {})
    now = time.time()
    rows = []
    selected = shortlist.head(int(limit))
    if client is None or not callable(getattr(client, "depth", None)):
        for _, item in selected.iterrows():
            sym = str(item["Symbol"])
            fy_sym = _to_fyers_equity_symbol(sym)
            rows.append(_normalise_depth_row(sym, fy_sym, {"s": "error", "message": "FYERS client/depth() not supplied by app.py"}))
        return pd.DataFrame(rows, columns=columns)

    last_call = float(st.session_state.get("ma_fyers_depth_last_call", 0.0))
    for _, item in selected.iterrows():
        symbol = str(item["Symbol"])
        fyers_symbol = _to_fyers_equity_symbol(symbol)
        if not fyers_symbol:
            rows.append(_normalise_depth_row(symbol, "", {"s": "error", "message": "Unsupported/non-NSE symbol"}))
            continue
        cached = cache.get(fyers_symbol)
        if cached and now - cached.get("ts", 0) < FYERS_DEPTH_CACHE_TTL:
            rows.append(cached["row"])
            continue

        wait_for = FYERS_DEPTH_CALL_GAP - (time.time() - last_call)
        if wait_for > 0:
            time.sleep(wait_for)
        try:
            # FYERS API v3 market-depth endpoint; no history/quotes/orders are called here.
            response = client.depth(data={"symbol": fyers_symbol, "ohlcv_flag": "1"})
            row = _normalise_depth_row(symbol, fyers_symbol, response)
        except Exception as exc:
            row = _normalise_depth_row(symbol, fyers_symbol, {"s": "error", "message": str(exc)[:180]})
        last_call = time.time()
        st.session_state["ma_fyers_depth_last_call"] = last_call
        cache[fyers_symbol] = {"ts": last_call, "row": row}
        rows.append(row)

    st.session_state["ma_fyers_depth_cache"] = cache
    return pd.DataFrame(rows, columns=columns)

def _sort_results(result):
    result = result.copy()
    result["_priority"] = result["Final Signal"].map(SIGNAL_PRIORITY).fillna(9)
    return result.sort_values(
        ["_priority", "Confirmation Strength %", "Symbol"],
        ascending=[True, False, True],
    ).drop(columns="_priority").reset_index(drop=True)


def _direction_movement(row):
    """Summarise 5M/15M/1H price direction; flag opposing FYERS book pressure."""
    directions = [str(row.get(tf, "NO DATA")).upper() for tf in ("5M", "15M", "1H")]
    buy_votes = sum(x == "BUY" for x in directions)
    sell_votes = sum(x == "SELL" for x in directions)

    if buy_votes >= 2 and sell_votes == 0:
        movement = "UP"
    elif sell_votes >= 2 and buy_votes == 0:
        movement = "DOWN"
    elif buy_votes == 0 and sell_votes == 0 and all(x == "WAIT" for x in directions):
        movement = "SIDEWAYS"
    elif directions.count("NO DATA") >= 2:
        movement = "NO DATA"
    else:
        movement = "MIXED"

    # FYERS depth is a displayed order-book snapshot, not executed trade flow.
    depth = str(row.get("Depth Signal", "") or "").upper()
    if movement == "UP" and depth == "SELL-SIDE DOMINANT":
        return "CONFLICT (UP / FYERS SELL)"
    if movement == "DOWN" and depth == "BUY-SIDE DOMINANT":
        return "CONFLICT (DOWN / FYERS BUY)"
    if movement == "UP" and depth == "BUY-SIDE DOMINANT":
        return "UP (DEPTH ALIGNED)"
    if movement == "DOWN" and depth == "SELL-SIDE DOMINANT":
        return "DOWN (DEPTH ALIGNED)"
    return movement


def show_ai_multi_agent_scanner(client=None):
    """Streamlit page. client is retained for app.py compatibility."""
    st.title("🤖 AI Multi-Agent Scanner — Yahoo Batch")
    st.caption("17 OHLCV rule engines × 5M / 15M / 1H + 20-candle 1M early-movement detector + optional Yahoo News")
    st.info("scanner.py supplies NSE/F&O symbols only (unchanged). Yahoo Finance supplies candles. FYERS is used only for 5-level Market Depth on the technical shortlist.")
    st.caption("Smart Memory: SQLite candles are loaded first; after the initial warm-up, the scanner refreshes a short 2-day overlap and merges it with saved candles. Yahoo 1-minute history is limited, so the first 1M warm-up uses 7 days.")
    st.warning(
        "Yahoo intraday data may be delayed, missing or rate-limited. "
        "Confirmation Strength is a heuristic consensus score, not a probability or win rate."
    )
    if yf is None:
        st.error("Missing dependency: add yfinance>=0.2.50 to requirements.txt and redeploy.")
        return
    try:
        from scanner import load_nse_equity_symbols, load_fo_stocks
    except Exception as exc:
        st.error(f"Could not import symbol loaders from scanner.py: {exc}")
        st.info("scanner.py must provide load_nse_equity_symbols() and load_fo_stocks().")
        return

    c1, c2, c3 = st.columns(3)
    with c1:
        universe = st.selectbox("Universe", ["Both (NSE + F&O)", "F&O Stocks", "NSE Equity"], key="ma_universe")
    with c2:
        batch_size = st.selectbox("Batch size", [25, 40, 50, 75, 100], index=2, key="ma_batch_size")
    with c3:
        st.metric("Analysis", "17 Agents × 3 TF")

    try:
        if universe == "Both (NSE + F&O)":
            # Keep scanner.py untouched: call its existing loaders and combine here.
            raw_symbols = _normalise_symbols(load_nse_equity_symbols()) + _normalise_symbols(load_fo_stocks())
        elif universe == "F&O Stocks":
            raw_symbols = _normalise_symbols(load_fo_stocks())
        else:
            raw_symbols = _normalise_symbols(load_nse_equity_symbols())
        symbols = _normalise_symbols(raw_symbols)
    except Exception as exc:
        st.error(f"Symbol list error: {exc}")
        return
    symbols = [s for s in symbols if not _is_index_symbol(s)]
    if not symbols:
        st.error("No stock symbols returned by scanner.py.")
        return

    seen, unique_items = set(), []
    for original in symbols:
        ticker = _to_yahoo_symbol(original)
        if ticker not in seen:
            seen.add(ticker)
            unique_items.append((original, ticker))
    st.caption(
        f"scanner.py symbols: {len(symbols)} • Unique Yahoo tickers: {len(unique_items)}. "
        "Full Scan attempts every unique ticker in this universe."
    )

    a, b, c = st.columns(3)
    with a:
        period = st.selectbox("Yahoo history", ["59d", "30d"], index=0, key="ma_period")
    with b:
        news_enabled = st.checkbox("News Intelligence (shortlist only)", value=True, key="ma_news_enabled")
    with c:
        news_limit = st.selectbox("News limit", [25, 50, 100, 200], index=1, key="ma_news_limit")

    depth_limit = st.selectbox(
        "FYERS Market Depth shortlist limit", [5, 10, 20, 30, 50], index=2,
        help="FYERS Market Depth is requested only for the strongest BUY/SELL/PRE-BIG rows. Calls are paced and cached.",
        key="ma_depth_limit",
    )

    auto_col1, auto_col2 = st.columns(2)
    with auto_col1:
        auto_scan = st.checkbox("⏱️ On-Time Auto Scan (market hours only)", value=False, key="ma_auto_scan")
    with auto_col2:
        auto_interval = st.selectbox("Auto Scan Interval", [5, 10, 15, 30], index=2, key="ma_auto_interval")

    # Streamlit fragments periodically request an app rerun. The scan itself still
    # runs through the same guarded batch path as the manual button.
    if hasattr(st, "fragment"):
        @st.fragment(run_every=f"{int(auto_interval)}m" if auto_scan else None)
        def _auto_scan_clock():
            if auto_scan and _is_market_hours():
                st.session_state["ma_auto_scan_due"] = True
                st.rerun(scope="app")
        _auto_scan_clock()
    elif auto_scan:
        st.warning("ఈ Streamlit version‌లో scheduled fragment లేదు. Auto Scan కోసం Streamlit 1.37+ అవసరం.")

    run_scan_now = st.button("🚀 RUN FULL BATCH SCAN", type="primary", key="run_multi_agent", **_stretch())
    auto_due = bool(st.session_state.pop("ma_auto_scan_due", False))
    if run_scan_now or (auto_due and auto_scan and _is_market_hours()):
        batches = [unique_items[i:i + batch_size] for i in range(0, len(unique_items), batch_size)]
        progress, status = st.progress(0.0), st.empty()
        all_rows = []
        for idx, batch in enumerate(batches, 1):
            status.write(f"Scanning batch {idx}/{len(batches)} — {len(batch)} symbols")
            try:
                all_rows.extend(_scan_batch(batch, period))
            except Exception as exc:
                all_rows.extend(_no_data_row(s, t, f"Batch failed: {exc}") for s, t in batch)
            progress.progress(idx / max(len(batches), 1))
            if idx < len(batches):
                time.sleep(.5)

        result = _sort_results(pd.DataFrame(all_rows))
        if news_enabled:
            candidates = result[result["Final Signal"].isin(
                ["BUY", "SELL", "PRE-BIG BUY", "PRE-BIG SELL"]
            )].head(news_limit)
            news_map = {}
            if len(candidates):
                news_bar = st.progress(0.0)
                for n, (_, item) in enumerate(candidates.iterrows(), 1):
                    status.write(f"News context {n}/{len(candidates)}: {item['Symbol']}")
                    news_map[item["Symbol"]] = _news_agent(item["Yahoo Symbol"])
                    news_bar.progress(n / len(candidates))
            default = {"direction": "NOT CHECKED", "score": 0, "reason": "Not in technical shortlist / news limit"}
            result["News Agent"] = result["Symbol"].map(lambda s: news_map.get(s, default)["direction"])
            result["News Score"] = result["Symbol"].map(lambda s: news_map.get(s, default)["score"])
            result["News Reason"] = result["Symbol"].map(lambda s: news_map.get(s, default)["reason"])
        else:
            result["News Agent"], result["News Score"], result["News Reason"] = "DISABLED", 0, "News disabled"

        # FYERS is called ONLY for the technical BUY/SELL/PRE-BIG shortlist.
        depth_candidates = result[result["Final Signal"].isin(
            ["BUY", "SELL", "PRE-BIG BUY", "PRE-BIG SELL"]
        )].head(depth_limit)
        depth_table = _fetch_fyers_depth_shortlist(client, depth_candidates, depth_limit)
        if not depth_table.empty:
            depth_columns = [c for c in depth_table.columns if c != "Symbol"]
            result = result.merge(depth_table[["Symbol"] + depth_columns], on="Symbol", how="left")
        else:
            for col in ["FYERS Symbol", "Depth Status", "Depth Signal", "Best Bid", "Best Ask", "Spread",
                        "Buy Qty (5L)", "Sell Qty (5L)", "Depth Imbalance %", "Depth Time (IST)", "Depth Message"]:
                result[col] = None
        # Rows outside the technical shortlist must never receive FYERS depth data.
        result.loc[~result["Final Signal"].isin(["BUY", "SELL", "PRE-BIG BUY", "PRE-BIG SELL"]),
                   ["FYERS Symbol", "Depth Status", "Depth Signal", "Best Bid", "Best Ask", "Spread",
                    "Buy Qty (5L)", "Sell Qty (5L)", "Depth Imbalance %", "Depth Time (IST)", "Depth Message"]] = None
        st.session_state["multi_agent_depth"] = depth_table

        # Direction Movement is placed beside Final Signal. It uses 5M/15M/1H
        # candle direction and flags disagreement with FYERS displayed depth.
        result["Direction Movement"] = result.apply(_direction_movement, axis=1)

        stamp = datetime.now(IST)
        try:
            _save_scan_history(result, stamp.strftime("%Y-%m-%d %H:%M:%S %Z"))
        except Exception as memory_exc:
            st.warning(f"SQLite scan history save issue: {memory_exc}")
        csv_bytes = result.to_csv(index=False).encode("utf-8-sig")
        xlsx_bytes, xlsx_error = None, ""
        try:
            buffer = io.BytesIO()
            with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
                result.to_excel(writer, index=False, sheet_name="AI Scanner")
            xlsx_bytes = buffer.getvalue()
        except Exception as exc:
            xlsx_error = str(exc)

        st.session_state["multi_agent_results"] = result
        st.session_state["multi_agent_csv"] = csv_bytes
        st.session_state["multi_agent_xlsx"] = xlsx_bytes
        st.session_state["multi_agent_xlsx_error"] = xlsx_error
        st.session_state["multi_agent_scan_info"] = {
            "universe": universe, "source_symbols": len(symbols),
            "unique_tickers": len(unique_items), "batches": len(batches),
            "completed_at": stamp.strftime("%Y-%m-%d %H:%M:%S IST"),
            "file_stamp": stamp.strftime("%Y%m%d_%H%M%S"),
        }
        status.success(
            f"Scan completed: {len(result)} Yahoo rows; FYERS depth requested for "
            f"{len(depth_candidates)} shortlisted stocks (limit {depth_limit})."
        )

    result = st.session_state.get("multi_agent_results")
    if not isinstance(result, pd.DataFrame) or result.empty:
        st.info("No results yet. Press RUN FULL BATCH SCAN.")
        return

    if "Direction Movement" not in result.columns:
        result["Direction Movement"] = result.apply(_direction_movement, axis=1)
    # Backward compatibility for results retained in Streamlit session_state from an older version.
    early_defaults = {
        "20M Status": "NO DATA", "20M Direction": "WAIT", "20M Pre-Move Score": 0,
        "20M Range High": None, "20M Range Low": None, "20M Range %": None,
        "20M RVOL": None, "20M Pressure %": None, "20M Last Close": None,
        "20M Reason": "Run a fresh scan to calculate the 1M early-movement detector",
    }
    for col_name, default_value in early_defaults.items():
        if col_name not in result.columns:
            result[col_name] = default_value
    st.session_state["multi_agent_results"] = result

    info = st.session_state.get("multi_agent_scan_info", {})
    candle_count, scan_count, memory_symbols = _memory_stats()
    mem_cols = st.columns(3)
    mem_cols[0].metric("SQLite Candles", f"{candle_count:,}")
    mem_cols[1].metric("Saved Scan Rows", f"{scan_count:,}")
    mem_cols[2].metric("Memory Symbols", f"{memory_symbols:,}")
    st.caption(f"Permanent candle database: {MEMORY_DB_PATH.name} • Auto Scan: {'ON' if st.session_state.get('ma_auto_scan', False) else 'OFF'} (weekdays 09:15–15:30 IST)")
    st.caption(
        f"Last scan: {info.get('universe', '')} • scanner.py symbols {info.get('source_symbols', '')} "
        f"• unique Yahoo tickers {info.get('unique_tickers', len(result))} "
        f"• batches {info.get('batches', '')} • {info.get('completed_at', '')}"
    )

    # Data coverage is shown separately from the final signal count.
    coverage_cols = st.columns(5)
    coverage_cols[0].metric("Rows attempted", len(result))
    for col, tf in zip(coverage_cols[1:4], ("5M", "15M", "1H")):
        col.metric(f"{tf} valid data", int((result[f"{tf} Candles"] >= 30).sum()))
    coverage_cols[4].metric("20M data", int((result["20M Status"] != "NO DATA").sum()))

    counts = result["Final Signal"].value_counts()
    metric_cols = st.columns(6)
    for col, label in zip(metric_cols, ["BUY", "SELL", "PRE-BIG BUY", "PRE-BIG SELL", "WAIT", "NO DATA"]):
        col.metric(label, int(counts.get(label, 0)))

    filter_col1, filter_col2 = st.columns(2)
    with filter_col1:
        signal_filter = st.selectbox(
            "Show technical signals", ["ALL", "BUY", "SELL", "PRE-BIG BUY", "PRE-BIG SELL", "WAIT", "NO DATA"],
            key="ma_filter",
        )
    with filter_col2:
        early_filter = st.selectbox(
            "20M Early Movement", ["ALL", "WATCH", "CONFIRMED BREAKOUT", "WAIT", "NO DATA"],
            key="ma_early_filter",
        )
    view = result if signal_filter == "ALL" else result[result["Final Signal"] == signal_filter]
    if early_filter != "ALL":
        view = view[view["20M Status"] == early_filter]
    view = view.sort_values(["20M Pre-Move Score", "Confirmation Strength %"], ascending=[False, False])
    summary_cols = [
        "Symbol", "Final Signal", "Direction Movement", "Signal Time (IST)", "Entry Price", "Stop Loss", "15M ATR",
        "Confirmation Strength %", "Consensus Score",
        "20M Status", "20M Direction", "20M Pre-Move Score", "20M Range High", "20M Range Low",
        "20M Range %", "20M RVOL", "20M Pressure %", "20M Last Close", "20M Reason",
        "5M", "15M", "1H", "5M Score", "15M Score", "1H Score",
        "5M Candles", "15M Candles", "1H Candles", "Last Price", "15M RSI",
        "News Agent", "News Score", "News Reason", "Depth Status", "Depth Signal",
        "Best Bid", "Best Ask", "Spread", "Buy Qty (5L)", "Sell Qty (5L)",
        "Depth Imbalance %", "Depth Time (IST)", "Errors",
    ]
    st.dataframe(view[summary_cols], hide_index=True, **_stretch())

    st.subheader("📚 FYERS Live Market Depth — Shortlist Only")
    depth_view = st.session_state.get("multi_agent_depth")
    if isinstance(depth_view, pd.DataFrame) and not depth_view.empty:
        st.caption(
            f"FYERS depth requests: {len(depth_view)} • cache TTL: {FYERS_DEPTH_CACHE_TTL}s • "
            "one request per symbol, paced at approximately one call/second."
        )
        st.dataframe(depth_view, hide_index=True, **_stretch())
        st.caption(
            "Buy Qty / Sell Qty = sum of displayed first five bid/ask levels. "
            "Depth Imbalance = (Buy Qty − Sell Qty) ÷ total × 100. "
            "This is a displayed-book snapshot, not executed buying/selling or a guaranteed direction."
        )
    else:
        st.info("No BUY / SELL / PRE-BIG shortlist rows yet, or Market Depth has not been requested.")
    st.caption("CSV/Excel include all 51 timeframe-agent outputs, each agent reason, and the separate 20-candle 1M early-movement fields.")
    st.caption("Signal Time = latest completed 15M candle close (IST). Entry = 15M close. Stop Loss = 1.5 × 15M ATR; shown only for BUY/SELL and PRE-BIG signals.")

    file_stamp = info.get("file_stamp", datetime.now(IST).strftime("%Y%m%d_%H%M%S"))
    if st.session_state.get("multi_agent_csv"):
        st.download_button(
            "⬇️ Download Full CSV", st.session_state["multi_agent_csv"],
            file_name=f"multi_agent_full_scan_{file_stamp}.csv",
            mime="text/csv", key="ma_csv",
        )
    if st.session_state.get("multi_agent_xlsx"):
        st.download_button(
            "⬇️ Download Full Excel", st.session_state["multi_agent_xlsx"],
            file_name=f"multi_agent_full_scan_{file_stamp}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="ma_xlsx",
        )
    elif st.session_state.get("multi_agent_xlsx_error"):
        st.info(f"Excel export unavailable: {st.session_state['multi_agent_xlsx_error']}")

    incomplete = result[
        (result["5M Candles"] < 30) | (result["15M Candles"] < 30) | (result["1H Candles"] < 30)
    ]
    if not incomplete.empty:
        with st.expander(f"Missing / insufficient timeframe data ({len(incomplete)} stocks)"):
            st.dataframe(
                incomplete[["Symbol", "Yahoo Symbol", "5M Candles", "15M Candles", "1H Candles", "Errors"]],
                hide_index=True, **_stretch(),
            )

    with st.expander("17 Agents — logic and limitations"):
        st.write(
            "Accumulation, Distribution, Liquidity Sweep, Volume Expansion, VWAP, EMA Trend, "
            "RSI Momentum, Fair Value Gap, HH/HL Structure, LH/LL Structure, Pre-Big Movement, "
            "Chart Pattern, Volume Analysis, SMC Structure, Order Block, Buy/Sell Pressure and "
            "Momentum Divergence run on 5M, 15M and 1H candles. These are deterministic technical "
            "rules, not 17 independent AI language models. News is a separate keyword-based "
            "headline context check, not a standalone trade signal. Chart Pattern uses OHLCV, "
            "not chart screenshots. Signals are screening outputs, not guaranteed entries."
        )
        st.dataframe(pd.DataFrame({"Agent": AGENT_NAMES}), hide_index=True, **_stretch())
