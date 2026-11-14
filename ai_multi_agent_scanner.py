"""
AI Multi-Agent Scanner for NSE AI PRO V17
Yahoo Finance batch edition — revised.

- scanner.py only supplies the NSE / F&O symbol universe; this module combines both loaders without modifying scanner.py.
- Yahoo Finance supplies all OHLCV candles and technical-analysis data.
- FYERS is used ONLY for 5-level Market Depth on shortlisted BUY/SELL/PRE-BIG stocks.
- Data Quality Gate + 15M Market Regime context before the 17-agent decision.
- 17 OHLCV rule-based agents on completed 5M, 15M and 1H candles.
- Directional UP/DOWN early pre-move watch using completed 5M candles + 15M alignment.
- Separate 20-candle 1M compression / volume buildup / breakout detector.
- Optional Yahoo headline context for the strongest technical shortlist.
- Compatible entry point: show_ai_multi_agent_scanner(client=None)

requirements.txt:
    yfinance>=0.2.50
    openpyxl>=3.1.0
    scikit-learn>=1.4.0  # optional at import; required to train the Real AI Brain
"""

import io
import json
import math
import pickle
import re
import sqlite3
import threading
from contextlib import contextmanager
import time
import uuid
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
_SCAN_WORKERS = {}
_SCAN_WORKERS_LOCK = threading.Lock()
FYERS_DEPTH_CACHE_TTL = 20       # seconds; prevents repeated depth calls on Streamlit reruns
FYERS_DEPTH_CALL_GAP = 1.05      # conservative one-call-per-second pacing
FYERS_DEPTH_DEFAULT_LIMIT = 20  # configurable shortlist cap per scan



# SQLite candle memory: persists completed Yahoo candles between Streamlit reruns.
# On hosted ephemeral filesystems, use a persistent mounted disk for survival across redeploys.
MEMORY_DB_PATH = Path(__file__).with_name("ai_multi_agent_candle_memory.sqlite3")

_MEMORY_INIT_LOCK = threading.Lock()
_MEMORY_INITIALIZED = False


@contextmanager
def _memory_connect():
    """Yield a SQLite connection, commit/rollback, and always close it."""
    global _MEMORY_INITIALIZED
    conn = sqlite3.connect(str(MEMORY_DB_PATH), timeout=20)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        if not _MEMORY_INITIALIZED:
            with _MEMORY_INIT_LOCK:
                if not _MEMORY_INITIALIZED:
                    conn.execute("""CREATE TABLE IF NOT EXISTS candles (
                        symbol TEXT NOT NULL, timeframe TEXT NOT NULL, candle_time TEXT NOT NULL,
                        open REAL, high REAL, low REAL, close REAL, volume REAL, saved_at TEXT,
                        PRIMARY KEY(symbol, timeframe, candle_time))""")
                    conn.execute("""CREATE TABLE IF NOT EXISTS scan_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, scanned_at TEXT NOT NULL,
                        symbol TEXT NOT NULL, final_signal TEXT, direction_movement TEXT,
                        last_price REAL, yahoo_signal_time TEXT)""")
                    conn.execute("""CREATE TABLE IF NOT EXISTS scan_jobs (
                        job_id TEXT PRIMARY KEY, status TEXT NOT NULL, config_json TEXT NOT NULL,
                        total_symbols INTEGER NOT NULL DEFAULT 0,
                        completed_symbols INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                        error TEXT)""")
                    conn.execute("""CREATE TABLE IF NOT EXISTS scan_job_rows (
                        job_id TEXT NOT NULL, symbol TEXT NOT NULL, ticker TEXT NOT NULL,
                        payload BLOB NOT NULL, saved_at TEXT NOT NULL,
                        PRIMARY KEY(job_id, symbol))""")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_scan_jobs_status ON scan_jobs(status, updated_at)")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_scan_history_time ON scan_history(scanned_at)")
                    conn.commit()
                    _MEMORY_INITIALIZED = True
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


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
        # Bound local DB growth: retain 8 days of optional 1M bars and 65 days
        # of 5M/15M/1H bars (more than the longest configured Yahoo lookback).
        cutoff_1m = (pd.Timestamp.now(tz=IST) - pd.Timedelta(days=8)).isoformat()
        cutoff_core = (pd.Timestamp.now(tz=IST) - pd.Timedelta(days=65)).isoformat()
        conn.execute("DELETE FROM candles WHERE timeframe='1M' AND candle_time < ?", (cutoff_1m,))
        conn.execute("DELETE FROM candles WHERE timeframe!='1M' AND candle_time < ?", (cutoff_core,))
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

def _load_candle_memory_batch(symbols, timeframe):
    """Load one timeframe for a batch using one SQLite query/connection."""
    empty_cols = ["Time", "Open", "High", "Low", "Close", "Volume"]
    symbols = [str(s) for s in symbols]
    if not symbols:
        return {}
    try:
        placeholders = ",".join("?" for _ in symbols)
        with _memory_connect() as conn:
            rows = conn.execute(
                f"""SELECT symbol, candle_time, open, high, low, close, volume
                    FROM candles
                    WHERE timeframe=? AND symbol IN ({placeholders})
                    ORDER BY symbol, candle_time""",
                [str(timeframe)] + symbols,
            ).fetchall()
        grouped = {s: [] for s in symbols}
        for row in rows:
            grouped.setdefault(row[0], []).append(row[1:])
        output = {}
        for symbol in symbols:
            data = grouped.get(symbol, [])
            if not data:
                output[symbol] = pd.DataFrame(columns=empty_cols)
                continue
            frame = pd.DataFrame(data, columns=empty_cols)
            frame["Time"] = pd.to_datetime(frame["Time"], errors="coerce", utc=True).dt.tz_convert(IST)
            for col in ("Open", "High", "Low", "Close", "Volume"):
                frame[col] = pd.to_numeric(frame[col], errors="coerce")
            frame = frame.dropna(subset=empty_cols)
            output[symbol] = (
                frame.sort_values("Time").drop_duplicates("Time", keep="last").reset_index(drop=True)
            )
        return output
    except Exception:
        return {s: pd.DataFrame(columns=empty_cols) for s in symbols}


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
        # Keep the latest 180 days; old candle history has its own retention policy.
        conn.execute("DELETE FROM scan_history WHERE scanned_at < ?",
                     ((pd.Timestamp.now(tz=IST) - pd.Timedelta(days=180)).strftime("%Y-%m-%d %H:%M:%S"),))
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
    Regular bars must end by 15:30. The final 15-minute partial interval is
    excluded from the 60M feed so its low volume cannot distort 60M indicators.
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

    # A 15:15-starting 60M bar contains only 15 minutes of trading volume.
    # Exclude it rather than comparing it with full 60M bars (distorted RVOL).
    final_partial_60m = (minutes == 60) & (tod == pd.Timedelta(hours=15, minutes=15))
    session_end = out["Time"].dt.normalize() + SESSION_CLOSE
    valid_end = (same_day_end <= session_end) & (~final_partial_60m)
    completed = same_day_end <= now

    out = out.loc[session_start & session_before_close & valid_end & completed].copy()
    return out.reset_index(drop=True)


@st.cache_data(ttl=180, show_spinner=False, max_entries=16)
def _download_yahoo_batch_cached(tickers_tuple, interval, period):
    if yf is None:
        raise RuntimeError("yfinance is not installed. Add yfinance>=0.2.50.")
    with _YF_LOCK:
        data = yf.download(
            tickers=list(tickers_tuple), period=period, interval=interval,
            auto_adjust=False, actions=False, progress=False, threads=8,
            group_by="ticker", timeout=20,
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


def _liquidity_levels_and_sweeps(df, lookback=30):
    """Detect swing/equal-high and swing/equal-low liquidity, then confirm sweeps."""
    empty = {
        "Buy-side Liquidity": None, "Sell-side Liquidity": None,
        "Equal High Liquidity": "NO", "Equal Low Liquidity": "NO",
        "Upside Sweep": "NO", "Downside Sweep": "NO",
        "Liquidity Sweep Detail": "NO DATA",
    }
    if df is None or not isinstance(df, pd.DataFrame) or len(df) < max(lookback + 1, 25):
        return empty
    d = df.tail(lookback + 1).reset_index(drop=True)
    prior, last = d.iloc[:-1], d.iloc[-1]
    highs = prior["High"].astype(float).to_numpy()
    lows = prior["Low"].astype(float).to_numpy()
    close_prev = float(prior["Close"].iloc[-1])
    close_now = _safe_num(last["Close"])
    # Use confirmed local pivots (one candle on each side), excluding the live/latest bar.
    swing_highs, swing_lows = [], []
    for i in range(1, len(prior)-1):
        if highs[i] >= highs[i-1] and highs[i] > highs[i+1]:
            swing_highs.append(highs[i])
        if lows[i] <= lows[i-1] and lows[i] < lows[i+1]:
            swing_lows.append(lows[i])
    ref_highs = swing_highs or list(highs[-min(lookback, len(highs)):])
    ref_lows = swing_lows or list(lows[-min(lookback, len(lows)):])
    buy_liq, sell_liq = max(ref_highs), min(ref_lows)
    # Equal levels: two or more confirmed pivots clustered within 0.15%.
    eq_tol = max(abs(close_prev) * 0.0015, 1e-9)
    equal_high = any(abs(a-b) <= eq_tol for i,a in enumerate(swing_highs) for b in swing_highs[i+1:])
    equal_low = any(abs(a-b) <= eq_tol for i,a in enumerate(swing_lows) for b in swing_lows[i+1:])
    high, low = _safe_num(last["High"]), _safe_num(last["Low"])
    opn = _safe_num(last["Open"])
    span = max(high-low, 1e-9)
    upper_wick, lower_wick = high-max(opn, close_now), min(opn, close_now)-low
    vol_avg = _safe_num(prior["Volume"].tail(20).mean(), 0.0)
    rvol = _safe_num(last["Volume"] / vol_avg, 0.0) if vol_avg > 0 else 0.0
    upside = bool(high > buy_liq and close_now < buy_liq and upper_wick/span >= .30
                  and close_now < opn and rvol >= 1.05)
    downside = bool(low < sell_liq and close_now > sell_liq and lower_wick/span >= .30
                    and close_now > opn and rvol >= 1.05)
    if upside and downside:
        detail = "BOTH-SIDE SWEEP — WAIT FOR NEXT CANDLE"
    elif upside:
        detail = "SWEEP UP: BUY-SIDE LIQUIDITY TAKEN; BEARISH REJECTION"
    elif downside:
        detail = "SWEEP DOWN: SELL-SIDE LIQUIDITY TAKEN; BULLISH REJECTION"
    else:
        detail = "NO CONFIRMED SWEEP"
    return {
        "Buy-side Liquidity": round(buy_liq, 2), "Sell-side Liquidity": round(sell_liq, 2),
        "Equal High Liquidity": "YES" if equal_high else "NO",
        "Equal Low Liquidity": "YES" if equal_low else "NO",
        "Upside Sweep": "YES" if upside else "NO", "Downside Sweep": "YES" if downside else "NO",
        "Liquidity Sweep Detail": detail,
    }


def _breakout_breakdown_signal(df, lookback=20):
    """Require a completed candle close beyond the previous range with participation."""
    empty = {"Breakout Signal": "WAIT", "Breakout Level": None,
             "Breakdown Level": None, "Breakout Reason": "Insufficient completed candles"}
    if df is None or not isinstance(df, pd.DataFrame) or len(df) < lookback + 21:
        return empty
    d = df.tail(lookback + 21).reset_index(drop=True)
    base, last = d.iloc[-(lookback+1):-1], d.iloc[-1]
    resistance, support = _safe_num(base["High"].max(), np.nan), _safe_num(base["Low"].min(), np.nan)
    o,h,l,c = (_safe_num(last[k]) for k in ("Open","High","Low","Close"))
    span=max(h-l,1e-9); body=abs(c-o)/span; close_loc=(c-l)/span
    avg_vol=_safe_num(base["Volume"].tail(20).mean(),0.0)
    rvol=_safe_num(last["Volume"]/avg_vol,0.0) if avg_vol>0 else 0.0
    if c>resistance and c>o and body>=.55 and close_loc>=.75 and rvol>=1.5:
        sig, reason="BREAKOUT",f"Completed close above swing resistance {resistance:.2f}; body {body:.0%}; RVOL {rvol:.2f}x"
    elif c<support and c<o and body>=.55 and close_loc<=.25 and rvol>=1.5:
        sig, reason="BREAKDOWN",f"Completed close below swing support {support:.2f}; body {body:.0%}; RVOL {rvol:.2f}x"
    else:
        sig, reason="WAIT",f"No confirmed range break; body {body:.0%}; RVOL {rvol:.2f}x"
    return {"Breakout Signal":sig,"Breakout Level":round(resistance,2),
            "Breakdown Level":round(support,2),"Breakout Reason":reason}


def _multi_tf_breakout(frames):
    """A single timeframe trigger is provisional; require 2/3 aligned TFs for confirmation."""
    checks={tf:_breakout_breakdown_signal(frames.get(tf,pd.DataFrame())) for tf in TIMEFRAMES}
    ups=sum(v["Breakout Signal"]=="BREAKOUT" for v in checks.values())
    dns=sum(v["Breakout Signal"]=="BREAKDOWN" for v in checks.values())
    if ups>=2 and dns==0: signal="BREAKOUT CONFIRMED"
    elif dns>=2 and ups==0: signal="BREAKDOWN CONFIRMED"
    elif ups or dns: signal="PROVISIONAL — WAIT"
    else: signal="WAIT"
    return {"Multi-TF Breakout":signal,
            "5M Breakout":checks["5M"]["Breakout Signal"],
            "15M Breakout":checks["15M"]["Breakout Signal"],
            "1H Breakout":checks["1H"]["Breakout Signal"]}


def _breakout_retest_confirmation(df, level, direction, tolerance_pct=0.003):
    """Require a close beyond level, then a later candle retesting and holding it."""
    if df is None or not isinstance(df, pd.DataFrame) or len(df) < 3 or level is None:
        return "WAIT — NO RETEST DATA"
    level = _safe_num(level, np.nan)
    if not np.isfinite(level) or level <= 0:
        return "WAIT — NO RETEST DATA"
    d = df.sort_values("Time").drop_duplicates("Time", keep="last").tail(8).reset_index(drop=True)
    if len(d) < 3:
        return "WAIT — NO RETEST DATA"
    tol = abs(level) * float(tolerance_pct)
    prior = d.iloc[:-1]  # the latest candle must be after the breakout candle
    if direction == "UP":
        breakout_positions = np.flatnonzero(prior["Close"].astype(float).to_numpy() > level)
        if not len(breakout_positions):
            return "WAIT — NO PRIOR UPSIDE BREAKOUT"
        breakout_idx = int(breakout_positions[-1])
        after = d.iloc[breakout_idx + 1:]
        if after.empty:
            return "WAIT — RETEST NOT YET OCCURRED"
        touched = (after["Low"].astype(float) <= level + tol) & (after["High"].astype(float) >= level - tol)
        held = float(d["Close"].iloc[-1]) > level
        return "RETEST HELD" if bool(touched.any()) and held else "WAIT — RETEST NOT CONFIRMED"
    if direction == "DOWN":
        breakout_positions = np.flatnonzero(prior["Close"].astype(float).to_numpy() < level)
        if not len(breakout_positions):
            return "WAIT — NO PRIOR DOWNSIDE BREAKDOWN"
        breakout_idx = int(breakout_positions[-1])
        after = d.iloc[breakout_idx + 1:]
        if after.empty:
            return "WAIT — RETEST NOT YET OCCURRED"
        touched = (after["High"].astype(float) >= level - tol) & (after["Low"].astype(float) <= level + tol)
        held = float(d["Close"].iloc[-1]) < level
        return "RETEST REJECTED" if bool(touched.any()) and held else "WAIT — RETEST NOT CONFIRMED"
    return "WAIT — NO BREAKOUT"


def _last_session_close(now=None, minutes=None):
    """Latest expected weekday close; 60M data excludes the 15:15 partial bar."""
    now = pd.Timestamp(now if now is not None else pd.Timestamp.now(tz=IST))
    now = now.tz_localize(IST) if now.tzinfo is None else now.tz_convert(IST)
    day = now.normalize()
    if now.weekday() >= 5:
        day -= pd.Timedelta(days=now.weekday() - 4)
    elif now.time() < pd.Timestamp("09:15").time():
        day -= pd.Timedelta(days=1)
    while day.weekday() >= 5:
        day -= pd.Timedelta(days=1)
    expected_close = pd.Timedelta(hours=15, minutes=15) if minutes == 60 else SESSION_CLOSE
    return day + expected_close


def _cached_candles_are_fresh(frame, minutes, now=None):
    """Avoid Yahoo calls only when SQLite contains the latest expected completed bar."""
    if frame is None or frame.empty or "Time" not in frame.columns:
        return False
    now = pd.Timestamp(now if now is not None else pd.Timestamp.now(tz=IST))
    now = now.tz_localize(IST) if now.tzinfo is None else now.tz_convert(IST)
    try:
        last = pd.Timestamp(frame["Time"].iloc[-1])
        last = last.tz_localize(IST) if last.tzinfo is None else last.tz_convert(IST)
        close_time = (last.normalize() + SESSION_CLOSE
                      if minutes == 60 and last.hour == 15 and last.minute == 15
                      else last + pd.Timedelta(minutes=minutes))
        if not _is_market_hours(now.to_pydatetime()):
            # At/after close and on weekends, cached data must reach the most
            # recent expected session close, not merely have today's date.
            return close_time >= (_last_session_close(now, minutes=minutes) - pd.Timedelta(minutes=1))
        age = (now - close_time).total_seconds() / 60.0
        return -1.0 <= age < float(minutes)
    except Exception:
        return False



def _run_17_agents(df, timeframe=None):
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
    range_limit = {"5M": 1.5, "15M": 3.0, "1H": 5.0}.get(str(timeframe).upper(), 1.5)
    span = max(_safe_num(h.iloc[-1] - l.iloc[-1]), 1e-9)
    close_loc = (last - _safe_num(l.iloc[-1])) / span

    result["Accumulation"] = (
        _agent_result("BUY", 1, "Tight range; close near candle high")
        if range_pct <= range_limit and close_loc >= .65 and last >= _safe_num(c.iloc[-5])
        else _agent_result()
    )
    result["Distribution"] = (
        _agent_result("SELL", -1, "Tight range; close near candle low")
        if range_pct <= range_limit and close_loc <= .35 and last <= _safe_num(c.iloc[-5])
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
    bullish_ob = None
    bearish_ob = None
    # An opposing candle is an OB candidate only if a later displacement closes
    # beyond the candidate window structure; then price must return to that zone.
    for pos in range(len(ob_window) - 1, -1, -1):
        candidate = ob_window.iloc[pos]
        later = d.iloc[-(len(ob_window)-pos):] if pos < len(ob_window)-1 else d.iloc[0:0]
        if later.empty:
            continue
        if candidate["Close"] < candidate["Open"] and later["Close"].max() > candidate["High"]:
            bullish_ob = (float(candidate["Low"]), float(candidate["High"]))
            break
    for pos in range(len(ob_window) - 1, -1, -1):
        candidate = ob_window.iloc[pos]
        later = d.iloc[-(len(ob_window)-pos):] if pos < len(ob_window)-1 else d.iloc[0:0]
        if later.empty:
            continue
        if candidate["Close"] > candidate["Open"] and later["Close"].min() < candidate["Low"]:
            bearish_ob = (float(candidate["Low"]), float(candidate["High"]))
            break
    bullish_retest = bullish_ob is not None and bullish_ob[0] <= last <= bullish_ob[1] * 1.003
    bearish_retest = bearish_ob is not None and bearish_ob[0] * 0.997 <= last <= bearish_ob[1]
    bullish_confirm = bullish_retest and last >= _safe_num(o.iloc[-1])
    bearish_confirm = bearish_retest and last <= _safe_num(o.iloc[-1])
    if bullish_confirm and bearish_confirm:
        result["Order Block"] = _agent_result("WAIT", 0, "Conflicting bullish and bearish OB zones")
    elif bullish_confirm:
        result["Order Block"] = _agent_result("BUY", 2, f"Displacement-confirmed bullish OB retest {bullish_ob[0]:.2f}-{bullish_ob[1]:.2f}")
    elif bearish_confirm:
        result["Order Block"] = _agent_result("SELL", -2, f"Displacement-confirmed bearish OB retest {bearish_ob[0]:.2f}-{bearish_ob[1]:.2f}")
    else:
        result["Order Block"] = _agent_result("WAIT", 0, "No displacement-confirmed order-block retest")

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
    old_low_slice, new_low_slice = l.iloc[-10:-5], l.iloc[-5:]
    old_high_slice, new_high_slice = h.iloc[-10:-5], h.iloc[-5:]
    old_low_idx, new_low_idx = old_low_slice.idxmin(), new_low_slice.idxmin()
    old_high_idx, new_high_idx = old_high_slice.idxmax(), new_high_slice.idxmax()
    old_price_low, new_price_low = _safe_num(l.loc[old_low_idx]), _safe_num(l.loc[new_low_idx])
    old_price_high, new_price_high = _safe_num(h.loc[old_high_idx]), _safe_num(h.loc[new_high_idx])
    old_rsi_low, new_rsi_low = _safe_num(rsi_series.loc[old_low_idx], 50), _safe_num(rsi_series.loc[new_low_idx], 50)
    old_rsi_high, new_rsi_high = _safe_num(rsi_series.loc[old_high_idx], 50), _safe_num(rsi_series.loc[new_high_idx], 50)
    if new_price_low < old_price_low and new_rsi_low > old_rsi_low + 2:
        result["Momentum Divergence"] = _agent_result("BUY", 1, "Possible bullish RSI divergence at price swing lows")
    elif new_price_high > old_price_high and new_rsi_high < old_rsi_high - 2:
        result["Momentum Divergence"] = _agent_result("SELL", -1, "Possible bearish RSI divergence at price swing highs")
    else:
        result["Momentum Divergence"] = _agent_result("WAIT", 0, "No clear regular RSI divergence")

    return result


# News is contextual headline keyword scoring, not an AI model or standalone signal.
_POSITIVE = [r"profits?", r"growth", r"upgrades?", r"upgraded", r"order wins?", r"approval",
             r"record (?:profit|revenue|orders?|highs?)", r"buybacks?", r"dividends?", r"partnerships?", r"strong results",
             r"beats? estimates"]
_NEGATIVE = [r"loss(?:es)?", r"downgrades?", r"downgraded", r"fraud", r"default(?:s|ed)?",
             r"(?:regulatory|investigation) probe", r"bans?", r"banned", r"penalty", r"penalties", r"resignation",
             r"weak results", r"misses? estimates", r"debt (?:default|restructuring|crisis)"]
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


def _analyse_timeframe(df, timeframe=None):
    agents = _run_17_agents(df, timeframe=timeframe)
    score = int(sum(x["score"] for x in agents.values()))
    buy_votes = sum(x["direction"] in ("BUY", "PRE-BIG BUY") for x in agents.values())
    sell_votes = sum(x["direction"] in ("SELL", "PRE-BIG SELL") for x in agents.values())
    valid = sum(x["direction"] != "NO DATA" for x in agents.values())
    if valid < 8:
        direction = "NO DATA"
    elif score >= 7 and buy_votes >= 4 and buy_votes >= sell_votes + 3:
        direction = "BUY"
    elif score <= -7 and sell_votes >= 4 and sell_votes >= buy_votes + 3:
        direction = "SELL"
    else:
        direction = "WAIT"
    has_data = df is not None and not df.empty and "Close" in df.columns
    close = round(_safe_num(df["Close"].iloc[-1]), 2) if has_data else None
    rsi_value = round(_safe_num(_rsi(df["Close"].astype(float)).iloc[-1]), 2) if has_data and len(df) >= 15 else None
    return {"direction": direction, "score": score, "agents": agents,
            "close": close, "rsi": rsi_value, "candles": len(df) if has_data else 0}


def _prebig_5m_analysis(df):
    """Find 5M compression + rising volume before a move; never label a watch as confirmed."""
    empty = {
        "PRE-BIG Status": "NO DATA", "PRE-BIG Direction": "WAIT",
        "PRE-BIG Score": 0, "PRE-BIG Candles": 0,
        "PRE-BIG Range High": None, "PRE-BIG Range Low": None,
        "PRE-BIG Range Width %": None, "PRE-BIG Volume Build %": None,
        "PRE-BIG RVOL": None, "PRE-BIG Trigger": None,
        "PRE-BIG Reason": "Need at least 25 completed 5M candles",
    }
    if df is None or not isinstance(df, pd.DataFrame) or len(df) < 25:
        return empty
    full = df.sort_values("Time").drop_duplicates("Time", keep="last").reset_index(drop=True)
    session_vwap = _vwap_last_session(full)
    d = full.tail(25).reset_index(drop=True)
    prior = d.iloc[-25:-13]       # 12 candles before the compression window
    base = d.iloc[-13:-1]         # latest 12 completed candles; latest candle is trigger test
    last = d.iloc[-1]
    if len(prior) < 12 or len(base) < 12:
        return empty
    hi = _safe_num(base["High"].max(), np.nan)
    lo = _safe_num(base["Low"].min(), np.nan)
    prev_width = _safe_num(prior["High"].max() - prior["Low"].min(), 0.0)
    width = hi - lo
    close = _safe_num(last["Close"], np.nan)
    if not np.isfinite(hi) or not np.isfinite(lo) or width <= 0 or close <= 0:
        return empty
    width_pct = width / close * 100.0
    compression = prev_width > 0 and width <= prev_width * 0.70
    # Compare last 3 candles in the 12-candle base with the earlier 9 candles.
    vol_early = _safe_num(base["Volume"].iloc[:9].mean(), 0.0)
    vol_recent = _safe_num(base["Volume"].iloc[-3:].mean(), 0.0)
    volume_build = vol_recent / vol_early if vol_early > 0 else 0.0
    avg20 = _safe_num(d.iloc[:-1]["Volume"].tail(20).mean(), 0.0)
    last_vol = _safe_num(last["Volume"], 0.0)
    rvol = last_vol / avg20 if avg20 > 0 else 0.0
    midpoint = (hi + lo) / 2.0
    base_close = _safe_num(base["Close"].iloc[-1], close)
    try:
        vwap = _safe_num(session_vwap, np.nan)
    except Exception:
        vwap = np.nan
    recent_lows = base["Low"].astype(float).tail(4).to_numpy()
    recent_highs = base["High"].astype(float).tail(4).to_numpy()
    higher_lows = len(recent_lows) >= 3 and recent_lows[-1] >= recent_lows[0]
    lower_highs = len(recent_highs) >= 3 and recent_highs[-1] <= recent_highs[0]
    upper_zone = base_close >= lo + width * 0.65
    lower_zone = base_close <= lo + width * 0.35
    vwap_bull = np.isfinite(vwap) and base_close >= vwap
    vwap_bear = np.isfinite(vwap) and base_close <= vwap
    # Latest completed candle can independently confirm a range break.
    o, h, l, c = (_safe_num(last[k]) for k in ("Open", "High", "Low", "Close"))
    span = max(h-l, 1e-9)
    body = abs(c-o) / span
    close_loc = (c-l) / span
    breakout_up = c > hi and c > o and body >= 0.55 and close_loc >= 0.75 and rvol >= 1.5
    breakout_dn = c < lo and c < o and body >= 0.55 and close_loc <= 0.25 and rvol >= 1.5
    score = 0
    score += 30 if compression else 0
    score += 25 if volume_build >= 1.30 else 15 if volume_build >= 1.15 else 0
    score += 20 if rvol >= 1.50 else 10 if rvol >= 1.20 else 0
    bull_points = int(upper_zone) + int(higher_lows) + int(vwap_bull)
    bear_points = int(lower_zone) + int(lower_highs) + int(vwap_bear)
    score += 25 if max(bull_points, bear_points) >= 2 else 10 if max(bull_points, bear_points) == 1 else 0
    if breakout_up:
        status, direction, reason = "BULLISH BREAKOUT CONFIRMED", "UP", "Completed 5M close above compressed range with strong body and RVOL"
        trigger = round(hi, 2)
        score = max(score, 85)
    elif breakout_dn:
        status, direction, reason = "BEARISH BREAKDOWN CONFIRMED", "DOWN", "Completed 5M close below compressed range with strong body and RVOL"
        trigger = round(lo, 2)
        score = max(score, 85)
    elif compression and volume_build >= 1.30 and rvol >= 1.20 and bull_points >= 2:
        status, direction, reason, trigger = "PRE-BIG BULLISH WATCH", "UP", "12-candle compression + rising volume + bullish location/structure; breakout not yet confirmed", round(hi, 2)
    elif compression and volume_build >= 1.30 and rvol >= 1.20 and bear_points >= 2:
        status, direction, reason, trigger = "PRE-BIG BEARISH WATCH", "DOWN", "12-candle compression + rising volume + bearish location/structure; breakdown not yet confirmed", round(lo, 2)
    else:
        status, direction, reason, trigger = "WAIT", "WAIT", "Compression, volume and directional conditions are not aligned", None
    return {
        "PRE-BIG Status": status, "PRE-BIG Direction": direction,
        "PRE-BIG Score": int(min(100, score)), "PRE-BIG Candles": 12,
        "PRE-BIG Range High": round(hi, 2), "PRE-BIG Range Low": round(lo, 2),
        "PRE-BIG Range Width %": round(width_pct, 3),
        "PRE-BIG Volume Build %": round((volume_build-1)*100, 1),
        "PRE-BIG RVOL": round(rvol, 2), "PRE-BIG Trigger": trigger,
        "PRE-BIG Reason": reason,
    }



def _directional_pre_move_engine(df):
    """Early UP/DOWN watch from completed 5M candles; not a guaranteed signal."""
    empty = {
        "Directional Pre-Move": "NO DATA", "Pre-Move Direction": "WAIT",
        "UP Pre-Move Score": 0, "DOWN Pre-Move Score": 0,
        "Pre-Move Volume Build %": None, "Pre-Move Compression": "UNKNOWN",
        "Pre-Move 15M Alignment": "UNKNOWN",
        "Pre-Move Reason": "Need at least 26 completed 5M candles",
    }
    required = {"Time", "Open", "High", "Low", "Close", "Volume"}
    if df is None or not isinstance(df, pd.DataFrame) or len(df) < 26 or not required.issubset(df.columns):
        return empty
    try:
        full = df.sort_values("Time").drop_duplicates("Time", keep="last").reset_index(drop=True)
        session_vwap = _vwap_last_session(full)
        d = full.tail(26).reset_index(drop=True)
        prior, recent, last = d.iloc[-25:-13], d.iloc[-13:-1], d.iloc[-1]
        old_width = _safe_num(prior["High"].max() - prior["Low"].min(), 0.0)
        recent_high = _safe_num(recent["High"].max(), 0.0)
        recent_low = _safe_num(recent["Low"].min(), 0.0)
        recent_width = recent_high - recent_low
        close = _safe_num(last["Close"], 0.0)
        if old_width <= 0 or recent_width <= 0 or close <= 0:
            return empty

        compression = recent_width <= old_width * 0.80
        early_volume = _safe_num(recent["Volume"].iloc[:9].mean(), 0.0)
        recent_volume = _safe_num(recent["Volume"].iloc[-3:].mean(), 0.0)
        volume_build = recent_volume / early_volume if early_volume > 0 else 0.0
        lows = recent["Low"].astype(float).to_numpy()
        highs = recent["High"].astype(float).to_numpy()
        higher_lows = float(lows[-3:].mean()) > float(lows[:3].mean())
        lower_highs = float(highs[-3:].mean()) < float(highs[:3].mean())
        location = max(0.0, min(1.0, (close - recent_low) / recent_width))
        try:
            vwap = _safe_num(session_vwap, np.nan)
        except Exception:
            vwap = np.nan
        above_vwap = np.isfinite(vwap) and close > vwap
        below_vwap = np.isfinite(vwap) and close < vwap

        up_score = 0
        down_score = 0
        if compression:
            up_score += 20
            down_score += 20
        if volume_build >= 1.30:
            up_score += 20
            down_score += 20
        elif volume_build >= 1.15:
            up_score += 12
            down_score += 12
        if higher_lows:
            up_score += 20
        if lower_highs:
            down_score += 20
        if location >= 0.65:
            up_score += 20
        elif location <= 0.35:
            down_score += 20
        if above_vwap:
            up_score += 15
        if below_vwap:
            down_score += 15
        up_score, down_score = min(100, up_score), min(100, down_score)

        if close > recent_high or close < recent_low:
            status = "MOVE ALREADY STARTED"
            direction = "UP" if close > recent_high else "DOWN"
            reason = "Latest completed 5M close is outside the setup range; not an early watch"
        elif compression and volume_build >= 1.15 and up_score >= 65 and up_score >= down_score + 15:
            status, direction = "UP PRE-MOVE WATCH", "UP"
            reason = "Compression + volume buildup + bullish structure/location"
        elif compression and volume_build >= 1.15 and down_score >= 65 and down_score >= up_score + 15:
            status, direction = "DOWN PRE-MOVE WATCH", "DOWN"
            reason = "Compression + volume buildup + bearish structure/location"
        else:
            status, direction = "WAIT", "WAIT"
            reason = "Directional evidence is not sufficiently separated"

        return {
            "Directional Pre-Move": status, "Pre-Move Direction": direction,
            "UP Pre-Move Score": int(up_score), "DOWN Pre-Move Score": int(down_score),
            "Pre-Move Volume Build %": round((volume_build - 1) * 100, 1),
            "Pre-Move Compression": "YES" if compression else "NO",
            "Pre-Move 15M Alignment": "CHECKING",
            "Pre-Move Reason": reason,
        }
    except Exception as exc:
        result = empty.copy()
        result["Pre-Move Reason"] = "Pre-move calculation error: " + str(exc)[:140]
        return result


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


def _last_candle_close_time(frame, minutes):
    """Return the close time of the latest completed candle in IST."""
    if frame is None or frame.empty or "Time" not in frame.columns:
        return None
    try:
        stamp = pd.Timestamp(frame["Time"].iloc[-1])
        stamp = stamp.tz_localize(IST) if stamp.tzinfo is None else stamp.tz_convert(IST)
        # Yahoo labels the NSE final 60-minute partial bar at 15:15; it closes at 15:30.
        if minutes == 60 and stamp.hour == 15 and stamp.minute == 15:
            close_stamp = stamp.normalize() + SESSION_CLOSE
        else:
            close_stamp = stamp + pd.Timedelta(minutes=minutes)
        return close_stamp.strftime("%Y-%m-%d %H:%M:%S IST")
    except Exception:
        return None



def _data_quality_gate(frames, min_candles=30):
    """Validate core OHLCV frames before trusting a signal."""
    report = {}
    failures = []
    required = {"Time", "Open", "High", "Low", "Close", "Volume"}
    for tf, (_, minutes) in TIMEFRAMES.items():
        df = frames.get(tf, pd.DataFrame())
        issues = []
        if not isinstance(df, pd.DataFrame) or df.empty:
            issues.append("No candles")
            count = 0
            last_time = None
        else:
            count = len(df)
            missing = sorted(required - set(df.columns))
            if missing:
                issues.append("Missing columns: " + ", ".join(missing))
            if not missing:
                x = df.copy()
                for col in ("Open", "High", "Low", "Close", "Volume"):
                    x[col] = pd.to_numeric(x[col], errors="coerce")
                invalid = (
                    x[list(required - {"Time"})].isna().any(axis=1)
                    | (x[["Open", "High", "Low", "Close"]] <= 0).any(axis=1)
                    | (x["Volume"] < 0)
                    | (x["High"] < x[["Open", "Close", "Low"]].max(axis=1))
                    | (x["Low"] > x[["Open", "Close", "High"]].min(axis=1))
                )
                if bool(invalid.any()):
                    issues.append(f"{int(invalid.sum())} invalid OHLCV rows")
                if "Time" in x.columns:
                    stamps = pd.to_datetime(x["Time"], errors="coerce")
                    if stamps.isna().any():
                        issues.append("Invalid candle timestamps")
                    if stamps.duplicated().any():
                        issues.append("Duplicate candle timestamps")
                    last_time = stamps.iloc[-1]
                else:
                    last_time = None
            else:
                last_time = None
            if count < min_candles:
                issues.append(f"Only {count} candles; need {min_candles}")

            # Staleness is checked only during NSE cash-market hours. Outside
            # market hours the last completed candle is expected to be older.
            if last_time is not None and not pd.isna(last_time) and _is_market_hours():
                try:
                    stamp = pd.Timestamp(last_time)
                    stamp = stamp.tz_localize(IST) if stamp.tzinfo is None else stamp.tz_convert(IST)
                    now_local = pd.Timestamp.now(tz=IST)
                    age_min = max(0.0, (now_local - stamp).total_seconds() / 60.0)
                    allowed = {"5M": 25, "15M": 45, "1H": 100}.get(tf, max(30, minutes * 2))
                    elapsed_from_open = (now_local - (now_local.normalize() + SESSION_OPEN)).total_seconds() / 60.0
                    before_first_bar = (
                        stamp.date() < now_local.date()
                        and 0 <= elapsed_from_open <= (minutes + 3)
                    )
                    if age_min > allowed and not before_first_bar:
                        issues.append(f"Latest candle is stale ({age_min:.0f} min old)")
                except Exception:
                    issues.append("Could not validate latest candle time")
        report[tf] = {
            "count": count,
            "status": "PASS" if not issues else "FAIL",
            "reason": "; ".join(issues) if issues else "OHLCV checks passed",
        }
        if issues:
            failures.append(f"{tf}: " + "; ".join(issues))

    return {
        "status": "PASS" if not failures else "FAIL",
        "reason": " | ".join(failures) if failures else "All 5M/15M/1H frames passed",
        "frames": report,
    }


def _market_regime_analysis(df):
    """Describe 15M market regime; informational only, never forces a trade."""
    empty = {
        "regime": "UNKNOWN", "strength": 0,
        "reason": "Need at least 30 valid 15M candles",
        "adx": None, "atr_pct": None, "bb_width_pct": None,
    }
    if df is None or not isinstance(df, pd.DataFrame) or len(df) < 30:
        return empty
    try:
        d = df.sort_values("Time").drop_duplicates("Time", keep="last").copy()
        h = pd.to_numeric(d["High"], errors="coerce")
        l = pd.to_numeric(d["Low"], errors="coerce")
        c = pd.to_numeric(d["Close"], errors="coerce")
        if c.isna().any() or c.iloc[-1] <= 0:
            return empty

        up_move = h.diff()
        down_move = -l.diff()
        plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
        minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
        prev_close = c.shift(1)
        tr = pd.concat([(h-l), (h-prev_close).abs(), (l-prev_close).abs()], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
        plus_di = 100 * plus_dm.ewm(alpha=1/14, adjust=False, min_periods=14).mean() / atr.replace(0, np.nan)
        minus_di = 100 * minus_dm.ewm(alpha=1/14, adjust=False, min_periods=14).mean() / atr.replace(0, np.nan)
        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
        adx = _safe_num(dx.ewm(alpha=1/14, adjust=False, min_periods=14).mean().iloc[-1], 0.0)
        atr_pct = _safe_num((atr.iloc[-1] / c.iloc[-1]) * 100, 0.0)

        mid = c.rolling(20, min_periods=20).mean()
        std = c.rolling(20, min_periods=20).std(ddof=0)
        upper, lower = mid + 2*std, mid - 2*std
        bb_width = _safe_num(((upper.iloc[-1] - lower.iloc[-1]) / mid.iloc[-1]) * 100, 0.0) if pd.notna(mid.iloc[-1]) and mid.iloc[-1] else 0.0
        ema20 = c.ewm(span=20, adjust=False).mean().iloc[-1]
        ema50 = c.ewm(span=50, adjust=False).mean().iloc[-1]

        if atr_pct >= 0.8 or bb_width >= 2.5:
            regime = "HIGH VOLATILITY"
            strength = min(100, int(round(max(atr_pct / 0.8, bb_width / 2.5) * 50)))
            reason = f"Elevated ATR% ({atr_pct:.2f}) / Bollinger width ({bb_width:.2f})"
        elif adx >= 22 and c.iloc[-1] > ema20 > ema50 and plus_di.iloc[-1] > minus_di.iloc[-1]:
            regime, strength = "TRENDING UP", min(100, int(adx * 2))
            reason = f"ADX {adx:.1f}; price and EMAs aligned upward"
        elif adx >= 22 and c.iloc[-1] < ema20 < ema50 and minus_di.iloc[-1] > plus_di.iloc[-1]:
            regime, strength = "TRENDING DOWN", min(100, int(adx * 2))
            reason = f"ADX {adx:.1f}; price and EMAs aligned downward"
        elif adx < 18 and bb_width < 5.0:
            regime, strength = "SIDEWAYS / COMPRESSION", max(0, min(100, int((18-adx)*4)))
            reason = f"Low ADX {adx:.1f}; narrow Bollinger width {bb_width:.2f}"
        else:
            regime, strength = "TRANSITION", min(100, int(adx * 2))
            reason = f"Mixed trend/volatility readings; ADX {adx:.1f}"

        return {
            "regime": regime, "strength": int(strength), "reason": reason,
            "adx": round(adx, 2), "atr_pct": round(atr_pct, 3),
            "bb_width_pct": round(bb_width, 3),
        }
    except Exception as exc:
        result = empty.copy()
        result["reason"] = "Regime calculation error: " + str(exc)[:140]
        return result


def _make_result_row(symbol, yahoo_symbol, payload):
    frames = payload.get("frames", {})
    quality = _data_quality_gate(frames)
    regime = _market_regime_analysis(frames.get("15M", pd.DataFrame()))
    analyses = {tf: _analyse_timeframe(frames.get(tf, pd.DataFrame()), timeframe=tf) for tf in TIMEFRAMES}
    valid = quality["status"] == "PASS" and all(
        analyses[tf]["direction"] != "NO DATA" for tf in TIMEFRAMES
    )

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
        strength = min(100, round(abs(weighted) / 20 * 100))

    # Signal timestamp is the close time of the latest completed 15M candle.
    # Entry/SL are populated only for actionable BUY/SELL signals.
    entry_price = None
    stop_loss = None
    atr_15m = None
    frame_5m = frames.get("5M", pd.DataFrame())
    frame_15m = frames.get("15M", pd.DataFrame())
    frame_1h = frames.get("1H", pd.DataFrame())
    # Signal Time is explicitly the latest completed 15M candle CLOSE, not scan-run time.
    signal_time = _last_candle_close_time(frame_15m, 15)
    candle_close_5m = _last_candle_close_time(frame_5m, 5)
    candle_close_15m = _last_candle_close_time(frame_15m, 15)
    candle_close_1h = _last_candle_close_time(frame_1h, 60)
    if frame_15m is not None and not frame_15m.empty:
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
        "5M Candle Close (IST)": candle_close_5m,
        "15M Candle Close (IST)": candle_close_15m,
        "1H Candle Close (IST)": candle_close_1h,
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
        "Data Quality": quality["status"],
        "Data Quality Reason": quality["reason"],
        "Market Regime": regime["regime"],
        "Regime Strength": regime["strength"],
        "Regime ADX": regime["adx"],
        "Regime ATR %": regime["atr_pct"],
        "Regime BB Width %": regime["bb_width_pct"],
        "Regime Reason": regime["reason"],
    }
    # Causal 5M features are shared by the ML training and inference paths.
    ml_features = _real_ai_feature_frame(frame_5m)
    if not ml_features.empty:
        for feature_name in AI_FEATURE_COLUMNS:
            value = ml_features[feature_name].iloc[-1]
            row[feature_name] = float(value) if pd.notna(value) else None
    else:
        for feature_name in AI_FEATURE_COLUMNS:
            row[feature_name] = None
    row.update(_early_20m_analysis(payload.get("early_1m", pd.DataFrame())))
    row.update(_prebig_5m_analysis(frame_5m))
    row.update(_directional_pre_move_engine(frame_5m))
    pre_direction = row.get("Pre-Move Direction", "WAIT")
    direction_15m = analyses["15M"]["direction"]
    if pre_direction == "UP":
        row["Pre-Move 15M Alignment"] = (
            "ALIGNED" if direction_15m == "BUY"
            else "NOT ALIGNED" if direction_15m == "SELL" else "WAIT"
        )
    elif pre_direction == "DOWN":
        row["Pre-Move 15M Alignment"] = (
            "ALIGNED" if direction_15m == "SELL"
            else "NOT ALIGNED" if direction_15m == "BUY" else "WAIT"
        )
    else:
        row["Pre-Move 15M Alignment"] = "NOT APPLICABLE"
    for tf in TIMEFRAMES:
        liquidity = _liquidity_levels_and_sweeps(frames.get(tf, pd.DataFrame()))
        for key, value in liquidity.items():
            row[f"{tf} {key}"] = value
    # Keep simple unprefixed fields for the main report, using 15M as the reference.
    row.update(_liquidity_levels_and_sweeps(frame_15m))
    row.update(_breakout_breakdown_signal(frame_15m))
    row.update(_multi_tf_breakout(frames))
    main_break = row.get("Breakout Signal", "WAIT")
    up_retest = _breakout_retest_confirmation(frame_15m, row.get("Breakout Level"), "UP")
    down_retest = _breakout_retest_confirmation(frame_15m, row.get("Breakdown Level"), "DOWN")
    if up_retest == "RETEST HELD":
        row["Breakout Retest"] = up_retest
    elif down_retest == "RETEST REJECTED":
        row["Breakout Retest"] = down_retest
    elif main_break == "BREAKOUT":
        row["Breakout Retest"] = up_retest
    elif main_break == "BREAKDOWN":
        row["Breakout Retest"] = down_retest
    else:
        row["Breakout Retest"] = "WAIT — NO RECENT BREAKOUT RETEST"
    # Explicit event label is separate from the trend consensus. A sweep is a
    # liquidity event, not an automatic BUY/SELL entry; breakout needs confirmation.
    sweep_detail = str(row.get("Liquidity Sweep Detail", ""))
    if row.get("Multi-TF Breakout") == "BREAKOUT CONFIRMED":
        row["Action Signal"] = "BREAKOUT"
    elif row.get("Multi-TF Breakout") == "BREAKDOWN CONFIRMED":
        row["Action Signal"] = "BREAKDOWN"
    elif row.get("Upside Sweep") == "YES":
        row["Action Signal"] = "SWEEP UP"
    elif row.get("Downside Sweep") == "YES":
        row["Action Signal"] = "SWEEP DOWN"
    elif final_signal in ("BUY", "SELL"):
        row["Action Signal"] = final_signal
    else:
        row["Action Signal"] = "WAIT"
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


def _fetch_interval(tickers, interval, minutes, period, retries=1):
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
                time.sleep(0.6)
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


def _cache_gap_fetch_period(frame, full_period="60d"):
    """Choose a Yahoo overlap large enough to cover the gap since cached candles."""
    if frame is None or frame.empty or "Time" not in frame.columns:
        return full_period
    try:
        last = pd.Timestamp(frame["Time"].max())
        last = last.tz_localize(IST) if last.tzinfo is None else last.tz_convert(IST)
        now = pd.Timestamp.now(tz=IST)
        gap_days = max(0, (now.date() - last.date()).days)
        if gap_days <= 1:
            return "2d"
        if gap_days <= 4:
            return "5d"
        return full_period
    except Exception:
        return full_period


def _scan_batch(batch, period, include_early=True):
    tickers = [ticker for _, ticker in batch]
    per_tf, problems = {}, {ticker: [] for ticker in tickers}
    original_by_ticker = {ticker: original for original, ticker in batch}

    # If SQLite already has data for every ticker, fetch only a short overlap window.
    # First scan remains a full-history warm-up. The overlap refreshes late/adjusted bars.
    for label, (interval, minutes) in TIMEFRAMES.items():
        cached = _load_candle_memory_batch(
            [original_by_ticker[t] for t in tickers], label
        )
        cached = {ticker: cached.get(original_by_ticker[ticker], pd.DataFrame()) for ticker in tickers}
        stale_tickers = [t for t in tickers if not _cached_candles_are_fresh(cached[t], minutes)]
        # Only request symbols whose saved candle set is stale or missing. This is
        # faster than downloading a 2-day overlap for every stock on every scan.
        if stale_tickers:
            fetch_periods = {_cache_gap_fetch_period(cached[t], period) for t in stale_tickers}
            # Batch symbols sharing the largest required overlap to avoid losing gaps.
            fetch_period = max(fetch_periods, key=lambda x: {"2d": 2, "5d": 5}.get(x, 999))
            fresh_frames, errors = _fetch_interval(stale_tickers, interval, minutes, fetch_period)
        else:
            fresh_frames, errors = {}, {}
        frames = {}
        for ticker in tickers:
            frames[ticker] = _merge_candle_frames(cached[ticker], fresh_frames.get(ticker))
            if frames[ticker].empty:
                problems[ticker].append(f"{label}: {errors.get(ticker, 'No cached or fresh data')}")
        per_tf[label] = frames

    # Optional 1-minute feed. It is the most expensive extra request set, so
    # the fast core scan can finish without downloading 1M candles for every stock.
    if include_early:
        early_cached_by_symbol = _load_candle_memory_batch(
            [original_by_ticker[t] for t in tickers], "1M"
        )
        early_cached = {
            ticker: early_cached_by_symbol.get(original_by_ticker[ticker], pd.DataFrame())
            for ticker in tickers
        }
        early_stale = [t for t in tickers if not _cached_candles_are_fresh(early_cached[t], 1)]
        if early_stale:
            early_periods = {_cache_gap_fetch_period(early_cached[t], "7d") for t in early_stale}
            early_period = max(early_periods, key=lambda x: {"2d": 2, "5d": 5}.get(x, 999))
            early_fresh, early_errors = _fetch_interval(early_stale, "1m", 1, early_period)
        else:
            early_fresh, early_errors = {}, {}
        early_frames = {
            ticker: _merge_candle_frames(early_cached[ticker], early_fresh.get(ticker))
            for ticker in tickers
        }
        per_tf["1M"] = early_frames
        for ticker in tickers:
            if early_frames[ticker].empty:
                problems[ticker].append(f"1M: {early_errors.get(ticker, 'No 1M data')}")
    else:
        per_tf["1M"] = {ticker: pd.DataFrame() for ticker in tickers}

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


def _best_stock_analyst(row):
    """18th agent: rank already-scanned stocks; never invent a trade signal.

    The score is a transparent screening score (0-100), not a probability.
    It rewards multi-timeframe alignment, agent consensus, volume/structure,
    and optional FYERS depth confirmation.
    """
    signal = str(row.get("Final Signal", "NO DATA")).upper()
    if signal in ("BUY", "PRE-BIG BUY"):
        side, expected = "BUY", "BUY"
    elif signal in ("SELL", "PRE-BIG SELL"):
        side, expected = "SELL", "SELL"
    else:
        return {
            "18th Agent": "REJECT / WAIT", "Best Stock Score": 0,
            "Best Stock Side": "WAIT", "Best Stock Grade": "NOT QUALIFIED",
            "Best Stock Reason": "No actionable BUY/SELL consensus",
            "Best Stock Filter": "REJECT",
        }

    # Hard reject missing or opposing timeframe confirmation.
    tfs = [str(row.get(tf, "NO DATA")).upper() for tf in ("5M", "15M", "1H")]
    aligned = sum(x == expected for x in tfs)
    opposing = sum(x in ("SELL", "PRE-BIG SELL") if expected == "BUY"
                   else x in ("BUY", "PRE-BIG BUY") for x in tfs)
    if aligned < 2 or opposing:
        return {
            "18th Agent": "REJECT / CONFLICT", "Best Stock Score": 0,
            "Best Stock Side": side, "Best Stock Grade": "NOT QUALIFIED",
            "Best Stock Reason": "Timeframes are mixed or an opposite timeframe exists",
            "Best Stock Filter": "REJECT",
        }

    strength = max(0.0, min(100.0, _safe_num(row.get("Confirmation Strength %"))))
    score_consensus = strength * 0.25                         # 25 points
    score_alignment = 25 if aligned == 3 else 18             # 25 points
    agent_votes = 0
    agent_total = 0
    for tf in ("5M", "15M", "1H"):
        for name in AGENT_NAMES:
            key = f"{tf} {name.replace(' ', '_').replace('/', '_')}"
            direction = str(row.get(key, "WAIT")).upper()
            if direction in ("BUY", "SELL", "PRE-BIG BUY", "PRE-BIG SELL"):
                agent_total += 1
                if (expected == "BUY" and direction in ("BUY", "PRE-BIG BUY")) or (
                    expected == "SELL" and direction in ("SELL", "PRE-BIG SELL")
                ):
                    agent_votes += 1
    vote_ratio = agent_votes / max(agent_total, 1)
    # Do not award a high consensus score from only one isolated agent vote.
    score_agents = (min(20.0, vote_ratio * 20.0) if agent_votes >= 3
                    else agent_votes / 3.0 * 10.0)             # 20 points

    volume_dirs = [str(row.get(f"{tf} Volume_Analysis", "WAIT")).upper()
                   for tf in ("15M", "1H")]
    volume_aligned = sum(
        d in (("BUY", "PRE-BIG BUY") if expected == "BUY" else ("SELL", "PRE-BIG SELL"))
        for d in volume_dirs
    )
    score_volume = 10 if volume_aligned == 2 else 5 if volume_aligned == 1 else 0

    structure_names = ("SMC_Structure", "Order_Block", "Buy_Sell_Pressure")
    structure_votes = 0
    for tf in ("15M", "1H"):
        for name in structure_names:
            d = str(row.get(f"{tf} {name}", "WAIT")).upper()
            if d in (("BUY", "PRE-BIG BUY") if expected == "BUY" else ("SELL", "PRE-BIG SELL")):
                structure_votes += 1
    score_structure = min(10.0, structure_votes / 6.0 * 10.0)

    depth = str(row.get("Depth Signal", "") or "").upper()
    if depth == ("BUY-SIDE DOMINANT" if expected == "BUY" else "SELL-SIDE DOMINANT"):
        score_depth, depth_note = 10, "FYERS depth aligned"
    elif depth in ("BUY-SIDE DOMINANT", "SELL-SIDE DOMINANT"):
        score_depth, depth_note = 0, "FYERS depth opposing"
    else:
        score_depth, depth_note = 5, "FYERS depth unavailable/neutral"

    total = int(round(score_consensus + score_alignment + score_agents
                      + score_volume + score_structure + score_depth))
    total = max(0, min(100, total))
    qualified = total >= 75 and opposing == 0
    grade = "A+" if total >= 90 else "A" if total >= 85 else "B" if total >= 75 else "BELOW 75"
    reasons = [
        f"{aligned}/3 timeframes aligned", f"agent agreement {vote_ratio:.0%}",
        f"15M/1H volume confirmation {volume_aligned}/2", depth_note,
    ]
    return {
        "18th Agent": "QUALIFIED" if qualified else "WATCHLIST",
        "Best Stock Score": total, "Best Stock Side": side,
        "Best Stock Grade": grade,
        "Best Stock Reason": "; ".join(reasons),
        "Best Stock Filter": "PASS" if qualified else "BELOW 75",
    }


def _direction_movement(row):
    """Summarise 5M/15M/1H direction, including PRE-BIG votes.

    FYERS depth is a displayed order-book snapshot, not executed trade flow;
    it annotates alignment/conflict but never changes the technical direction.
    """
    directions = [str(row.get(tf, "NO DATA") or "NO DATA").upper()
                  for tf in ("5M", "15M", "1H")]
    buy_votes = sum(x in ("BUY", "PRE-BIG BUY") for x in directions)
    sell_votes = sum(x in ("SELL", "PRE-BIG SELL") for x in directions)
    direct_buy = sum(x == "BUY" for x in directions)
    direct_sell = sum(x == "SELL" for x in directions)
    pre_buy = sum(x == "PRE-BIG BUY" for x in directions)
    pre_sell = sum(x == "PRE-BIG SELL" for x in directions)

    if buy_votes >= 2 and sell_votes == 0:
        movement = "PRE-BIG UP" if pre_buy >= 2 and direct_buy == 0 else "UP"
    elif sell_votes >= 2 and buy_votes == 0:
        movement = "PRE-BIG DOWN" if pre_sell >= 2 and direct_sell == 0 else "DOWN"
    elif buy_votes == 0 and sell_votes == 0 and all(x == "WAIT" for x in directions):
        movement = "SIDEWAYS"
    elif directions.count("NO DATA") >= 2:
        movement = "NO DATA"
    else:
        movement = "MIXED"

    depth = str(row.get("Depth Signal", "") or "").upper()
    is_up = movement in ("UP", "PRE-BIG UP")
    is_down = movement in ("DOWN", "PRE-BIG DOWN")
    if is_up and depth == "SELL-SIDE DOMINANT":
        return f"CONFLICT ({movement} / FYERS SELL)"
    if is_down and depth == "BUY-SIDE DOMINANT":
        return f"CONFLICT ({movement} / FYERS BUY)"
    if is_up and depth == "BUY-SIDE DOMINANT":
        return f"{movement} (DEPTH ALIGNED)"
    if is_down and depth == "SELL-SIDE DOMINANT":
        return f"{movement} (DEPTH ALIGNED)"
    return movement



def _scan_job_create(config):
    """Create a durable scan job and return its unique ID."""
    job_id = uuid.uuid4().hex
    now_text = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S %Z")
    config_text = json.dumps(config, ensure_ascii=False)
    with _memory_connect() as conn:
        conn.execute(
            """INSERT INTO scan_jobs
               (job_id,status,config_json,total_symbols,completed_symbols,created_at,updated_at,error)
               VALUES (?,?,?,?,?,?,?,?)""",
            (job_id, "RUNNING", config_text, len(config.get("symbols", [])), 0,
             now_text, now_text, None),
        )
    return job_id


def _scan_job_get(job_id):
    if not job_id:
        return None
    try:
        with _memory_connect() as conn:
            row = conn.execute(
                """SELECT job_id,status,config_json,total_symbols,completed_symbols,
                          created_at,updated_at,error
                   FROM scan_jobs WHERE job_id=?""", (str(job_id),)
            ).fetchone()
        if not row:
            return None
        return {
            "job_id": row[0], "status": row[1], "config": json.loads(row[2]),
            "total": int(row[3]), "completed": int(row[4]),
            "created_at": row[5], "updated_at": row[6], "error": row[7],
        }
    except Exception:
        return None


def _scan_job_latest_active():
    try:
        with _memory_connect() as conn:
            row = conn.execute(
                """SELECT job_id FROM scan_jobs
                   WHERE status IN ('RUNNING','PAUSED','DONE')
                   ORDER BY created_at DESC LIMIT 1"""
            ).fetchone()
        return row[0] if row else None
    except Exception:
        return None


def _scan_job_load_rows(job_id):
    """Restore completed technical rows in original scan-universe order."""
    job = _scan_job_get(job_id)
    if not job:
        return []
    config_symbols = job["config"].get("symbols", [])
    try:
        with _memory_connect() as conn:
            rows = conn.execute(
                "SELECT symbol,payload FROM scan_job_rows WHERE job_id=?",
                (str(job_id),),
            ).fetchall()
        payload_by_symbol = {
            symbol: pickle.loads(payload) for symbol, payload in rows
        }
        return [
            payload_by_symbol[item[0]]
            for item in config_symbols
            if item[0] in payload_by_symbol
        ]
    except Exception:
        return []


def _scan_job_worker(job_id):
    """Background scanner. Each finished batch is committed before moving on."""
    job = _scan_job_get(job_id)
    if not job:
        return
    config = job["config"]
    symbols = [(str(x[0]), str(x[1])) for x in config.get("symbols", [])]
    batch_size = max(10, min(int(config.get("batch_size", 25)), 100))
    period = str(config.get("period", "30d"))
    include_early = bool(config.get("include_early", False))
    try:
        with _memory_connect() as conn:
            done_rows = conn.execute(
                "SELECT symbol FROM scan_job_rows WHERE job_id=?", (job_id,)
            ).fetchall()
        completed_symbols = {x[0] for x in done_rows}
        pending = [x for x in symbols if x[0] not in completed_symbols]

        for start in range(0, len(pending), batch_size):
            batch = pending[start:start + batch_size]
            try:
                batch_rows = _scan_batch(batch, period, include_early=include_early)
                # A fully empty batch is usually a transient Yahoo throttling/network
                # failure. Retry once before committing permanent NO DATA checkpoints.
                if batch_rows and all(str(r.get("Final Signal", "")) == "NO DATA" for r in batch_rows):
                    time.sleep(1.0)
                    retry_rows = _scan_batch(batch, period, include_early=include_early)
                    if sum(str(r.get("Final Signal", "")) != "NO DATA" for r in retry_rows) > 0:
                        batch_rows = retry_rows
            except Exception as exc:
                batch_rows = [
                    _no_data_row(symbol, ticker, f"Batch failed: {str(exc)[:180]}")
                    for symbol, ticker in batch
                ]

            saved_at = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S %Z")
            payload_rows = [
                (job_id, str(row.get("Symbol", symbol)), str(row.get("Yahoo Symbol", ticker)),
                 sqlite3.Binary(pickle.dumps(row, protocol=pickle.HIGHEST_PROTOCOL)), saved_at)
                for (symbol, ticker), row in zip(batch, batch_rows)
            ]
            # Commit a whole batch atomically: after a crash, only an unfinished
            # batch is repeated. Completed symbols are never downloaded again.
            with _memory_connect() as conn:
                conn.executemany(
                    """INSERT INTO scan_job_rows(job_id,symbol,ticker,payload,saved_at)
                       VALUES (?,?,?,?,?)
                       ON CONFLICT(job_id,symbol) DO UPDATE SET
                         ticker=excluded.ticker,payload=excluded.payload,saved_at=excluded.saved_at""",
                    payload_rows,
                )
                total_done = conn.execute(
                    "SELECT COUNT(*) FROM scan_job_rows WHERE job_id=?", (job_id,)
                ).fetchone()[0]
                conn.execute(
                    """UPDATE scan_jobs SET status='RUNNING',completed_symbols=?,
                       updated_at=?,error=NULL WHERE job_id=?""",
                    (int(total_done), saved_at, job_id),
                )

        finished_at = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S %Z")
        with _memory_connect() as conn:
            total_done = conn.execute(
                "SELECT COUNT(*) FROM scan_job_rows WHERE job_id=?", (job_id,)
            ).fetchone()[0]
            conn.execute(
                """UPDATE scan_jobs SET status='DONE',completed_symbols=?,
                   updated_at=?,error=NULL WHERE job_id=?""",
                (int(total_done), finished_at, job_id),
            )
    except Exception as exc:
        paused_at = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S %Z")
        try:
            with _memory_connect() as conn:
                conn.execute(
                    """UPDATE scan_jobs SET status='PAUSED',updated_at=?,error=?
                       WHERE job_id=?""",
                    (paused_at, str(exc)[:1000], job_id),
                )
        except Exception:
            pass
    finally:
        with _SCAN_WORKERS_LOCK:
            _SCAN_WORKERS.pop(job_id, None)


def _scan_job_start_worker(job_id):
    """Start or resume one job; a live worker is never duplicated."""
    if not job_id:
        return False
    with _SCAN_WORKERS_LOCK:
        current = _SCAN_WORKERS.get(job_id)
        if current is not None and current.is_alive():
            return True
        job = _scan_job_get(job_id)
        if not job or job["status"] not in ("RUNNING", "PAUSED"):
            return False
        worker = threading.Thread(
            target=_scan_job_worker, args=(job_id,),
            name=f"nse-scan-{job_id[:8]}", daemon=True,
        )
        _SCAN_WORKERS[job_id] = worker
        worker.start()
        return True




# ============================================================
# REAL AI BRAIN — supervised ML layer (additive; keeps 17 agents)
# The 17 agents remain transparent technical feature generators.
# This model learns 3-bar forward direction from saved completed 5M candles.
# It is NOT an LLM and does not guarantee future performance.
# ============================================================
AI_FEATURE_COLUMNS = [
    "AI Ret 1", "AI Ret 3", "AI Ret 6", "AI Momentum 10",
    "AI Range %", "AI Body %", "AI Close Location", "AI RVOL 20",
    "AI EMA Gap %", "AI RSI", "AI Volume Change", "AI ATR %",
    "AI Compression", "AI VWAP Gap %",
]
AI_LABELS = {0: "DOWN", 1: "SIDEWAYS", 2: "UP"}


def _real_ai_feature_frame(frame):
    """Causal features only: each row uses that candle and earlier candles."""
    cols = ["Open", "High", "Low", "Close", "Volume"]
    if frame is None or frame.empty or not set(cols).issubset(frame.columns):
        return pd.DataFrame(columns=AI_FEATURE_COLUMNS)
    d = frame.copy()
    for col in cols:
        d[col] = pd.to_numeric(d[col], errors="coerce")
    o, h, l, c, v = (d[x] for x in cols)
    prev = c.shift(1)
    tr = pd.concat([(h-l), (h-prev).abs(), (l-prev).abs()], axis=1).max(axis=1)
    atr = tr.rolling(14, min_periods=7).mean()
    avg_vol = v.shift(1).rolling(20, min_periods=8).mean()
    ema9 = c.ewm(span=9, adjust=False).mean()
    ema21 = c.ewm(span=21, adjust=False).mean()
    typical = (h + l + c) / 3
    cum_pv = (typical * v).groupby(
        d["Time"].dt.date if "Time" in d.columns else pd.Series(0, index=d.index)
    ).cumsum()
    cum_v = v.groupby(
        d["Time"].dt.date if "Time" in d.columns else pd.Series(0, index=d.index)
    ).cumsum().replace(0, np.nan)
    session_vwap = cum_pv / cum_v
    candle_range = (h-l).replace(0, np.nan)
    close_loc = ((c-l) / candle_range).clip(0, 1)
    vol_ratio = v / avg_vol.replace(0, np.nan)
    range_pct = (h-l) / c.replace(0, np.nan) * 100
    rolling_range = (h.rolling(10, min_periods=5).max() -
                     l.rolling(10, min_periods=5).min()) / c.replace(0, np.nan) * 100
    prior_range = (h.rolling(30, min_periods=15).max() -
                   l.rolling(30, min_periods=15).min()) / c.replace(0, np.nan) * 100
    out = pd.DataFrame(index=d.index)
    out["AI Ret 1"] = c.pct_change(1) * 100
    out["AI Ret 3"] = c.pct_change(3) * 100
    out["AI Ret 6"] = c.pct_change(6) * 100
    out["AI Momentum 10"] = c.pct_change(10) * 100
    out["AI Range %"] = range_pct
    out["AI Body %"] = (c-o) / c.replace(0, np.nan) * 100
    out["AI Close Location"] = close_loc
    out["AI RVOL 20"] = vol_ratio.clip(0, 10)
    out["AI EMA Gap %"] = (ema9-ema21) / c.replace(0, np.nan) * 100
    out["AI RSI"] = _rsi(c) / 100.0
    out["AI Volume Change"] = v.pct_change().replace([np.inf, -np.inf], np.nan).clip(-5, 5)
    out["AI ATR %"] = atr / c.replace(0, np.nan) * 100
    out["AI Compression"] = rolling_range / prior_range.replace(0, np.nan)
    out["AI VWAP Gap %"] = (c-session_vwap) / c.replace(0, np.nan) * 100
    return out.replace([np.inf, -np.inf], np.nan)


def _real_ai_training_data():
    """Build time-labelled examples from SQLite 5M candles; future bars are labels only."""
    samples = []
    try:
        with _memory_connect() as conn:
            symbols = [r[0] for r in conn.execute(
                "SELECT DISTINCT symbol FROM candles WHERE timeframe='5M'"
            ).fetchall()]
            for symbol in symbols:
                rows = conn.execute(
                    """SELECT candle_time,open,high,low,close,volume FROM candles
                       WHERE symbol=? AND timeframe='5M' ORDER BY candle_time""",
                    (symbol,),
                ).fetchall()
                if len(rows) < 45:
                    continue
                d = pd.DataFrame(rows, columns=["Time","Open","High","Low","Close","Volume"])
                d["Time"] = pd.to_datetime(d["Time"], errors="coerce", utc=True)
                feat = _real_ai_feature_frame(d)
                # Forward horizon stays inside one NSE session; no overnight label leakage.
                session_key = d["Time"].dt.tz_convert(IST).dt.date
                future_close = d["Close"].groupby(session_key).shift(-3)
                future_time = d["Time"].groupby(session_key).shift(-3)
                future_return = future_close / d["Close"] - 1
                atr_pct = feat["AI ATR %"] / 100.0
                deadband = np.maximum(0.0025, atr_pct * 0.50)
                label = pd.Series(np.where(future_return > deadband, 2,
                                  np.where(future_return < -deadband, 0, 1)), index=d.index)
                valid = feat.notna().all(axis=1) & future_return.notna() & future_time.notna()
                part = feat.loc[valid].copy()
                part["__label"] = label.loc[valid].astype(int)
                part["__time"] = d.loc[valid, "Time"].values
                part["__target_time"] = future_time.loc[valid].values
                samples.append(part)
    except Exception:
        return pd.DataFrame()
    if not samples:
        return pd.DataFrame()
    data = pd.concat(samples, ignore_index=True)
    return data.sort_values("__time").reset_index(drop=True)


def _train_real_ai_brain(force=False):
    """Train a time-ordered RandomForest model; optional sklearn dependency."""
    if not force and st.session_state.get("ma_real_ai_model"):
        return st.session_state["ma_real_ai_model"], None
    try:
        from sklearn.ensemble import RandomForestClassifier
        from sklearn.metrics import accuracy_score, balanced_accuracy_score
    except Exception as exc:
        return None, f"scikit-learn unavailable: {exc}"
    data = _real_ai_training_data()
    # Bound training cost while preserving the latest chronological samples.
    if len(data) > 300_000:
        data = data.tail(300_000).reset_index(drop=True)
    if len(data) < 500:
        return None, f"Training data insufficient: {len(data)} labelled samples; need at least 500."
    X = data[AI_FEATURE_COLUMNS].astype(float)
    y = data["__label"].astype(int)
    split = int(len(data) * 0.80)
    if split < 300 or len(data)-split < 100:
        return None, "Need at least 300 training rows and 100 later test rows."
    cutoff_time = pd.to_datetime(data.iloc[split]["__time"], utc=True)
    # Purge training samples whose 3-candle label horizon crosses into test time.
    train_mask = pd.to_datetime(data["__target_time"], utc=True) < cutoff_time
    test_mask = pd.to_datetime(data["__time"], utc=True) >= cutoff_time
    X_train, X_test = X.loc[train_mask], X.loc[test_mask]
    y_train, y_test = y.loc[train_mask], y.loc[test_mask]
    if len(X_train) < 300 or len(X_test) < 100:
        return None, "After purging the 3-candle boundary, need 300 train and 100 test rows."
    if y_train.nunique() < 3 or y_test.nunique() < 2:
        return None, "Historical labels do not contain enough direction classes."
    model = RandomForestClassifier(
        n_estimators=220, max_depth=8, min_samples_leaf=5,
        class_weight="balanced_subsample", random_state=42, n_jobs=2,
    )
    model.fit(X_train, y_train)
    predicted = model.predict(X_test)
    metrics = {
        "samples": int(len(data)), "train_samples": int(len(X_train)),
        "test_samples": int(len(X_test)),
        "accuracy": float(accuracy_score(y_test, predicted)),
        "balanced_accuracy": float(balanced_accuracy_score(y_test, predicted)),
        "trained_at": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST"),
    }
    return {"model": model, "metrics": metrics}, None


def _apply_real_ai_brain(result):
    """Attach model estimates in one batch; never overwrite the 17-agent signal."""
    result = result.copy()
    defaults = {
        "AI Direction": "NOT TRAINED", "AI Up Probability %": None,
        "AI Down Probability %": None, "AI Sideways Probability %": None,
        "AI Brain Signal": "MODEL NOT TRAINED", "AI Model Status": "NOT TRAINED",
    }
    for col, default in defaults.items():
        if col not in result.columns:
            result[col] = default
    bundle = st.session_state.get("ma_real_ai_model")
    if not bundle or not all(c in result.columns for c in AI_FEATURE_COLUMNS):
        return result
    try:
        x = result[AI_FEATURE_COLUMNS].apply(pd.to_numeric, errors="coerce")
        values = x.to_numpy(dtype=float)
        valid = np.isfinite(values).all(axis=1)
        if not valid.any():
            return result
        model = bundle["model"]
        prob_matrix = model.predict_proba(values[valid])
        class_to_col = {int(k): i for i, k in enumerate(model.classes_)}
        down = prob_matrix[:, class_to_col[0]] if 0 in class_to_col else np.zeros(len(prob_matrix))
        sideways = prob_matrix[:, class_to_col[1]] if 1 in class_to_col else np.zeros(len(prob_matrix))
        up = prob_matrix[:, class_to_col[2]] if 2 in class_to_col else np.zeros(len(prob_matrix))
        probs = np.column_stack([down, sideways, up])
        best_idx = probs.argmax(axis=1)
        labels = np.array(["DOWN", "SIDEWAYS", "UP"], dtype=object)[best_idx]
        confidence = probs.max(axis=1)
        row_indices = np.flatnonzero(valid)
        result.loc[result.index[row_indices], "AI Direction"] = labels
        result.loc[result.index[row_indices], "AI Down Probability %"] = np.round(down * 100, 1)
        result.loc[result.index[row_indices], "AI Sideways Probability %"] = np.round(sideways * 100, 1)
        result.loc[result.index[row_indices], "AI Up Probability %"] = np.round(up * 100, 1)
        result.loc[result.index[row_indices], "AI Model Status"] = "TRAINED"

        technical = result["Final Signal"].fillna("WAIT").astype(str).str.upper().to_numpy()
        balanced = _safe_num(bundle.get("metrics", {}).get("balanced_accuracy"), 0.0)
        reliable = balanced >= 0.45
        brain = np.full(len(result), "MODEL NOT TRAINED", dtype=object)
        brain[valid] = "WATCH — VALIDATION WEAK" if not reliable else "WATCH — LOW MODEL CONFIDENCE"
        if reliable:
            for local_i, global_i in enumerate(row_indices):
                side = labels[local_i]
                if confidence[local_i] < 0.60:
                    brain[global_i] = "WATCH — LOW MODEL CONFIDENCE"
                elif side == "UP" and technical[global_i] in ("BUY", "PRE-BIG BUY"):
                    brain[global_i] = "AI BUY"
                elif side == "DOWN" and technical[global_i] in ("SELL", "PRE-BIG SELL"):
                    brain[global_i] = "AI SELL"
                elif side in ("UP", "DOWN"):
                    brain[global_i] = "WATCH — TECHNICAL CONFLICT"
                else:
                    brain[global_i] = "WATCH — SIDEWAYS"
        result["AI Brain Signal"] = brain
    except Exception as exc:
        result["AI Model Status"] = "PREDICTION ERROR"
        result["AI Brain Signal"] = "WATCH — AI PREDICTION ERROR"
        result["AI Prediction Error"] = str(exc)[:160]
    return result



def show_ai_multi_agent_scanner(client=None):
    """Streamlit page. client is retained for app.py compatibility."""
    st.title("🤖 AI Multi-Agent Scanner V17 — 17 Agents + Real AI Brain Hybrid")
    st.caption("17 OHLCV agents × 5M / 15M / 1H + supervised ML AI Brain + optional 1M / News")
    st.info("scanner.py supplies NSE/F&O symbols only (unchanged). Yahoo Finance supplies candles. FYERS is used only for 5-level Market Depth on the technical shortlist.")
    st.caption("Smart Memory: SQLite candles are loaded first; after the initial warm-up, the scanner refreshes a short 2-day overlap and merges it with saved candles. For speed, the extra 1M/20M scan is optional.")
    st.warning(
        "Yahoo intraday data may be delayed, missing or rate-limited. "
        "17-Agent Confirmation Strength is a heuristic score, not a probability. ML probabilities are shown separately and only after historical validation."
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
        batch_size = st.selectbox("Batch size", [10, 25, 50, 75, 100], index=1, key="ma_batch_size_v18", help="Local PC: 25 is the safer default for NSE. Completed batches are checkpointed and can resume.")
    with c3:
        st.metric("Analysis", "17 Agents + ML Brain")

    try:
        if universe == "Both (NSE + F&O)":
            # PRIORITY FIX: scan the smaller, fast-moving F&O universe FIRST.
            # NSE equity scanning continues after F&O; scanner.py remains untouched.
            fo_symbols = _normalise_symbols(load_fo_stocks())
            nse_symbols = _normalise_symbols(load_nse_equity_symbols())
            raw_symbols = fo_symbols + nse_symbols
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
        "Priority: F&O stocks first, then NSE equity (when Both is selected). "
        "Full Scan attempts every unique ticker in this universe."
    )

    a, b, c = st.columns(3)
    with a:
        period = st.selectbox("Yahoo history", ["59d", "30d"], index=1, key="ma_period_v18")
    with b:
        news_enabled = st.checkbox("News Intelligence (shortlist only — slower)", value=False, key="ma_news_enabled")
    with c:
        news_limit = st.selectbox("News limit", [25, 50, 100, 200], index=1, key="ma_news_limit")

    early_scan_enabled = st.checkbox(
        "Run extra 1M / 20M Early-Movement scan for all stocks (slower)",
        value=False, key="ma_early_scan_enabled_v18",
        help="OFF = faster core scan on 5M/15M/1H. ON = also downloads Yahoo 1-minute candles for every stock.",
    )

    depth_limit = st.selectbox(
        "FYERS Market Depth shortlist limit", [5, 10, 20, 30, 50], index=1,
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
                now_epoch = time.time()
                last_trigger = float(st.session_state.get("ma_auto_scan_last_trigger", 0.0))
                interval_seconds = int(auto_interval) * 60
                # The fragment also runs during a full app rerun. Initialise its
                # clock first, then trigger only after the selected interval; this
                # prevents st.rerun(scope='app') from creating a rerun loop.
                if last_trigger <= 0:
                    st.session_state["ma_auto_scan_last_trigger"] = now_epoch
                elif now_epoch - last_trigger >= interval_seconds:
                    st.session_state["ma_auto_scan_last_trigger"] = now_epoch
                    st.session_state["ma_auto_scan_due"] = True
                    st.rerun(scope="app")
        _auto_scan_clock()
    elif auto_scan:
        st.warning("ఈ Streamlit version‌లో scheduled fragment లేదు. Auto Scan కోసం Streamlit 1.37+ అవసరం.")

    run_scan_now = st.button("🚀 RUN FULL BATCH SCAN", type="primary", key="run_multi_agent", **_stretch())
    auto_due = bool(st.session_state.pop("ma_auto_scan_due", False))
    start_scan_requested = run_scan_now or (auto_due and auto_scan and _is_market_hours())
    resume_scan_now = st.button("▶️ RESUME / CONTINUE SCAN", key="ma_resume_scan", **_stretch())

    # Reconnect to the latest unfinished scan after a rerun, refresh or browser reopen.
    job_id = st.session_state.get("ma_scan_job_id")
    job = _scan_job_get(job_id) if job_id else None
    if not job or job["status"] in ("FINALIZED", "CANCELLED"):
        active_job_id = _scan_job_latest_active()
        if active_job_id:
            job_id = active_job_id
            st.session_state["ma_scan_job_id"] = job_id
            job = _scan_job_get(job_id)

    if start_scan_requested:
        if job and job["status"] in ("RUNNING", "PAUSED"):
            st.warning("ఒక scan ఇప్పటికే pending/running లో ఉంది. దాన్ని Resume చేయండి; duplicate scan ప్రారంభించలేదు.")
        else:
            config = {
                "universe": universe,
                "source_symbols": len(symbols),
                "symbols": [[original, ticker] for original, ticker in unique_items],
                "batch_size": int(batch_size),
                "period": period,
                "include_early": bool(early_scan_enabled),
                "news_enabled": bool(news_enabled),
                "news_limit": int(news_limit),
                "depth_limit": int(depth_limit),
            }
            job_id = _scan_job_create(config)
            st.session_state["ma_scan_job_id"] = job_id
            st.session_state.pop("ma_scan_finalized_job", None)
            job = _scan_job_get(job_id)
            _scan_job_start_worker(job_id)

    if resume_scan_now and job_id:
        _scan_job_start_worker(job_id)
        job = _scan_job_get(job_id)

    # The worker continues independently of browser rendering. This lightweight
    # fragment only polls SQLite; a minimized browser may throttle display refresh,
    # but does not stop the local Python worker.
    if job_id and job and job["status"] in ("RUNNING", "PAUSED"):
        if job["status"] == "RUNNING":
            _scan_job_start_worker(job_id)
        if hasattr(st, "fragment"):
            @st.fragment(run_every="1s")
            def _scan_job_monitor():
                current = _scan_job_get(job_id)
                if not current:
                    st.warning("Scan job record not found.")
                    return
                total = max(current["total"], 1)
                done = min(current["completed"], total)
                st.progress(done / total, text=f"Background scan: {done:,}/{current['total']:,} stocks")
                st.caption(
                    f"Status: {current['status']} • Job: {job_id[:8]} • "
                    "F&O priority order preserved; finished batches saved to SQLite."
                )
                if current["status"] == "PAUSED":
                    st.error(f"Scan paused: {current.get('error') or 'Unknown worker error'}. Press RESUME / CONTINUE SCAN.")
                elif current["status"] == "DONE":
                    st.success("Technical scan completed. Preparing the existing reports…")
                    st.rerun(scope="app")
            _scan_job_monitor()
        else:
            st.info("Background scan is running. Install/upgrade Streamlit to enable live progress refresh.")
        job = _scan_job_get(job_id)

    job_done = bool(job and job["status"] == "DONE")
    already_finalized = st.session_state.get("ma_scan_finalized_job") == job_id
    run_scan_now = job_done and not already_finalized

    if job and job["status"] == "PAUSED":
        st.warning("Scan paused safely. Completed batches are saved; press RESUME / CONTINUE SCAN to continue.")
    elif job and job["status"] == "RUNNING":
        st.info("NSE/F&O scan is running in the background. You can minimize the browser; the local Python worker continues.")
    elif job and job["status"] == "DONE" and already_finalized:
        st.success("Latest scan is already processed. Existing report and Excel downloads are available below.")

    if run_scan_now:
        saved_job = _scan_job_get(job_id)
        universe = saved_job["config"].get("universe", universe)
        unique_items = [(str(x[0]), str(x[1])) for x in saved_job["config"].get("symbols", [])]
        symbols = [x[0] for x in unique_items]
        batch_size = int(saved_job["config"].get("batch_size", batch_size))
        period = str(saved_job["config"].get("period", period))
        early_scan_enabled = bool(saved_job["config"].get("include_early", early_scan_enabled))
        news_enabled = bool(saved_job["config"].get("news_enabled", news_enabled))
        news_limit = int(saved_job["config"].get("news_limit", news_limit))
        depth_limit = int(saved_job["config"].get("depth_limit", depth_limit))
        all_rows = _scan_job_load_rows(job_id)
        batches = [
            saved_job["config"]["symbols"][i:i + int(saved_job["config"]["batch_size"])]
            for i in range(0, len(saved_job["config"]["symbols"]), int(saved_job["config"]["batch_size"]))
        ]
        status = st.empty()
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

        # Direction Movement includes BUY/SELL and PRE-BIG direction votes.
        # FYERS displayed depth only annotates alignment/conflict; it is not flow.
        result["Direction Movement"] = result.apply(_direction_movement, axis=1)
        # 18th Agent runs after technical consensus and optional FYERS depth are available.
        analyst = result.apply(_best_stock_analyst, axis=1, result_type="expand")
        result = pd.concat([result, analyst], axis=1)

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
                # A separate first sheet makes the downloaded workbook open as a dashboard.
                counts = result.get("Action Signal", pd.Series(dtype=str)).fillna("WAIT").value_counts()
                dash_rows = [
                    ["AI MULTI-AGENT SCANNER DASHBOARD", ""],
                    ["Generated (IST)", stamp.strftime("%Y-%m-%d %H:%M:%S IST")],
                    ["Total Stocks Scanned", int(len(result))],
                    ["BUY", int(counts.get("BUY", 0))],
                    ["SELL", int(counts.get("SELL", 0))],
                    ["SWEEP UP", int(counts.get("SWEEP UP", 0))],
                    ["SWEEP DOWN", int(counts.get("SWEEP DOWN", 0))],
                    ["BREAKOUT", int(counts.get("BREAKOUT", 0))],
                    ["BREAKDOWN", int(counts.get("BREAKDOWN", 0))],
                    ["WAIT", int(counts.get("WAIT", 0))],
                    ["", ""],
                    ["TOP QUALIFIED STOCKS", ""],
                ]
                dashboard = pd.DataFrame(dash_rows, columns=["Metric", "Value"])
                dashboard.to_excel(writer, index=False, header=False, sheet_name="Dashboard", startrow=0)
                top_cols = [c for c in ["Symbol", "Action Signal", "Final Signal", "Best Stock Score", "Confirmation Strength %", "Last Price", "Breakout Signal", "Multi-TF Breakout", "Breakout Retest"] if c in result.columns]
                top = result.copy()
                if "Best Stock Filter" in top.columns:
                    top = top[top["Best Stock Filter"] == "PASS"]
                if "Best Stock Score" in top.columns:
                    top = top.sort_values("Best Stock Score", ascending=False)
                top[top_cols].head(15).to_excel(writer, index=False, sheet_name="Dashboard", startrow=len(dash_rows)+1)
                from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
                from openpyxl.formatting.rule import CellIsRule
                wb = writer.book
                ws = wb["Dashboard"]
                ws.freeze_panes = "A3"
                ws.column_dimensions["A"].width = 30
                ws.column_dimensions["B"].width = 34
                for col in range(3, max(ws.max_column, 2)+1):
                    ws.column_dimensions[chr(64+col)].width = 22
                ws.merge_cells("A1:B1")
                ws["A1"].font = Font(bold=True, size=16, color="FFFFFF")
                ws["A1"].fill = PatternFill("solid", fgColor="17365D")
                ws["A1"].alignment = Alignment(horizontal="center")
                ws[2][0].font = Font(bold=True, color="17365D")
                ws[3][0].font = Font(bold=True, color="17365D")
                # Style the dashboard metric labels and the top-stocks header.
                for r in range(3, 11):
                    ws.cell(r,1).font = Font(bold=True)
                header_row = len(dash_rows)+2
                for cell in ws[header_row]:
                    cell.font = Font(bold=True, color="FFFFFF")
                    cell.fill = PatternFill("solid", fgColor="305496")
                for row_cells in ws.iter_rows(min_row=header_row+1):
                    for cell in row_cells:
                        cell.alignment = Alignment(vertical="center")
                from openpyxl.chart import BarChart, Reference
                chart = BarChart()
                chart.type = "bar"
                chart.style = 10
                chart.title = "Signal Counts"
                chart.y_axis.title = "Signal"
                chart.x_axis.title = "Stocks"
                chart.height, chart.width = 7, 12
                chart.add_data(Reference(ws, min_col=2, min_row=4, max_row=10), titles_from_data=False)
                chart.set_categories(Reference(ws, min_col=1, min_row=4, max_row=10))
                ws.add_chart(chart, "D3")
                # Main scanner report: filters, freeze panes, readable widths and colored signals.
                report = wb["AI Scanner"]
                report.freeze_panes = "A2"
                report.auto_filter.ref = report.dimensions
                for cell in report[1]:
                    cell.font = Font(bold=True, color="FFFFFF")
                    cell.fill = PatternFill("solid", fgColor="17365D")
                    cell.alignment = Alignment(wrap_text=True, vertical="center")
                report.row_dimensions[1].height = 32
                for col_cells in report.columns:
                    letter = col_cells[0].column_letter
                    header = str(col_cells[0].value or "")
                    report.column_dimensions[letter].width = min(max(len(header)+2, 13), 28)
                headers = {str(c.value): c.column for c in report[1]}
                for key in ("Action Signal", "Final Signal", "Breakout Signal", "Multi-TF Breakout"):
                    if key in headers:
                        col_letter = report.cell(1, headers[key]).column_letter
                        report.conditional_formatting.add(f"{col_letter}2:{col_letter}{report.max_row}",
                            CellIsRule(operator="equal", formula=['"BUY"'], fill=PatternFill("solid", fgColor="C6EFCE")))
                        report.conditional_formatting.add(f"{col_letter}2:{col_letter}{report.max_row}",
                            CellIsRule(operator="equal", formula=['"SELL"'], fill=PatternFill("solid", fgColor="F4CCCC")))
                # Open the workbook on the Dashboard sheet by default.
                wb._sheets = [wb["Dashboard"], wb["AI Scanner"]]
            # Capture the completed workbook bytes for Streamlit download.
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
        st.session_state["ma_scan_finalized_job"] = job_id
        with _memory_connect() as conn:
            conn.execute(
                "UPDATE scan_jobs SET status='FINALIZED',updated_at=? WHERE job_id=?",
                (datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S %Z"), job_id),
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
    directional_pre_move_defaults = {
        "Directional Pre-Move": "NOT CALCULATED",
        "Pre-Move Direction": "WAIT",
        "UP Pre-Move Score": 0,
        "DOWN Pre-Move Score": 0,
        "Pre-Move Volume Build %": None,
        "Pre-Move Compression": "UNKNOWN",
        "Pre-Move 15M Alignment": "UNKNOWN",
        "Pre-Move Reason": "Run a fresh scan to calculate directional pre-move",
    }
    for col_name, default_value in directional_pre_move_defaults.items():
        if col_name not in result.columns:
            result[col_name] = default_value
    quality_regime_defaults = {
        "Data Quality": "NOT CALCULATED",
        "Data Quality Reason": "Run a fresh scan",
        "Market Regime": "UNKNOWN",
        "Regime Strength": 0,
        "Regime ADX": None,
        "Regime ATR %": None,
        "Regime BB Width %": None,
        "Regime Reason": "Run a fresh scan",
    }
    for col_name, default_value in quality_regime_defaults.items():
        if col_name not in result.columns:
            result[col_name] = default_value

    prebig_defaults = {
        "PRE-BIG Status": "NO DATA", "PRE-BIG Direction": "WAIT", "PRE-BIG Score": 0,
        "PRE-BIG Candles": 0, "PRE-BIG Range High": None, "PRE-BIG Range Low": None,
        "PRE-BIG Range Width %": None, "PRE-BIG Volume Build %": None,
        "PRE-BIG RVOL": None, "PRE-BIG Trigger": None,
        "PRE-BIG Reason": "Run a fresh scan to calculate 5M PRE-BIG detector",
    }
    for col_name, default_value in prebig_defaults.items():
        if col_name not in result.columns:
            result[col_name] = default_value
    liquidity_defaults = {
        "Buy-side Liquidity": None, "Sell-side Liquidity": None,
        "Upside Sweep": "NO DATA", "Downside Sweep": "NO DATA",
        "Equal High Liquidity": "NO DATA", "Equal Low Liquidity": "NO DATA",
        "Liquidity Sweep Detail": "Run a fresh scan for liquidity levels",
    }
    for tf in TIMEFRAMES:
        for key, default in liquidity_defaults.items():
            col_name = f"{tf} {key}"
            if col_name not in result.columns:
                result[col_name] = default
    for key, default in liquidity_defaults.items():
        if key not in result.columns:
            result[key] = default
    new_signal_defaults = {
        "Breakout Signal": "WAIT", "Breakout Level": None,
        "Breakdown Level": None, "Breakout Reason": "Run a fresh scan",
        "Action Signal": "WAIT", "Multi-TF Breakout": "WAIT",
        "5M Breakout": "WAIT", "15M Breakout": "WAIT", "1H Breakout": "WAIT",
        "Breakout Retest": "WAIT — NO RETEST DATA",
    }
    for col_name, default_value in new_signal_defaults.items():
        if col_name not in result.columns:
            result[col_name] = default_value
    if "Depth Signal" not in result.columns:
        result["Depth Signal"] = None
    if "Confirmation Strength %" not in result.columns:
        result["Confirmation Strength %"] = 0
    if "Best Stock Score" not in result.columns:
        analyst = result.apply(_best_stock_analyst, axis=1, result_type="expand")
        result = pd.concat([result, analyst], axis=1)
    st.session_state["multi_agent_results"] = result

    info = st.session_state.get("multi_agent_scan_info", {})
    candle_count, scan_count, memory_symbols = _memory_stats()
    mem_cols = st.columns(3)
    mem_cols[0].metric("SQLite Candles", f"{candle_count:,}")
    mem_cols[1].metric("Saved Scan Rows", f"{scan_count:,}")
    mem_cols[2].metric("Memory Symbols", f"{memory_symbols:,}")
    st.caption(f"Local SQLite candle database: {MEMORY_DB_PATH.name} • Host persistence depends on the deployment disk • Auto Scan: {'ON' if st.session_state.get('ma_auto_scan', False) else 'OFF'} (weekdays 09:15–15:30 IST)")
    st.caption(
        f"Last scan: {info.get('universe', '')} • scanner.py symbols {info.get('source_symbols', '')} "
        f"• unique Yahoo tickers {info.get('unique_tickers', len(result))} "
        f"• batches {info.get('batches', '')} • {info.get('completed_at', '')}"
    )

    st.subheader("⚡ EARLY DIRECTIONAL PRE-MOVE — UP / DOWN")
    pre_watch = result[
        result["Directional Pre-Move"].isin(["UP PRE-MOVE WATCH", "DOWN PRE-MOVE WATCH"])
        & result["Pre-Move Direction"].isin(["UP", "DOWN"])
        & (result["Pre-Move 15M Alignment"] == "ALIGNED")
    ].copy()
    pre_watch_cols = [
        "Symbol", "Directional Pre-Move", "Pre-Move Direction",
        "UP Pre-Move Score", "DOWN Pre-Move Score",
        "Pre-Move Volume Build %", "Pre-Move Compression",
        "Pre-Move 15M Alignment", "Market Regime", "Signal Time (IST)",
        "Last Price", "Pre-Move Reason",
    ]
    if not pre_watch.empty:
        pre_watch = pre_watch.sort_values(
            ["Pre-Move 15M Alignment", "UP Pre-Move Score", "DOWN Pre-Move Score"],
            ascending=[True, False, False],
        )
        st.dataframe(pre_watch[pre_watch_cols], hide_index=True, **_stretch())
    else:
        st.caption("No directional pre-move watch currently meets the setup threshold.")

    with st.expander("Data Quality + Market Regime checks"):
        st.write(
            "Data Quality checks the 5M/15M/1H OHLCV frames, minimum candle count, "
            "invalid prices/volume, duplicate timestamps and intraday staleness. "
            "Market Regime is descriptive context only; it does not force BUY/SELL."
        )
        quality_cols = ["Symbol", "Data Quality", "Data Quality Reason", "Market Regime", "Regime Strength", "Regime ADX", "Regime ATR %", "Regime BB Width %"]
        st.dataframe(result[quality_cols].head(100), hide_index=True, **_stretch())

    # Real AI Brain training uses saved, completed 5M candles and chronological holdout.
    st.subheader("🧠 REAL AI BRAIN — Hybrid ML")
    ai_col1, ai_col2 = st.columns(2)
    with ai_col1:
        train_ai_now = st.button("🧠 TRAIN / REFRESH REAL AI BRAIN", key="ma_train_real_ai")
    with ai_col2:
        ai_bundle = st.session_state.get("ma_real_ai_model")
        if ai_bundle:
            st.metric("AI Balanced Accuracy (time holdout)", f"{ai_bundle['metrics']['balanced_accuracy']*100:.1f}%")
        else:
            st.metric("AI Model", "NOT TRAINED")
    if train_ai_now:
        with st.spinner("Training ML Brain from saved completed 5M candles..."):
            trained_bundle, train_error = _train_real_ai_brain(force=True)
        if trained_bundle:
            st.session_state["ma_real_ai_model"] = trained_bundle
            st.session_state["ma_real_ai_train_error"] = None
            st.success(
                f"AI Brain trained: {trained_bundle['metrics']['samples']:,} samples • "
                f"test balanced accuracy {trained_bundle['metrics']['balanced_accuracy']*100:.1f}% "
                f"• trained {trained_bundle['metrics']['trained_at']}"
            )
        else:
            st.session_state["ma_real_ai_model"] = None
            st.session_state["ma_real_ai_train_error"] = train_error
    ai_bundle = st.session_state.get("ma_real_ai_model")
    if ai_bundle:
        st.caption(
            f"Chronological 80/20 validation • {ai_bundle['metrics']['train_samples']:,} train / "
            f"{ai_bundle['metrics']['test_samples']:,} test • accuracy "
            f"{ai_bundle['metrics']['accuracy']*100:.1f}% • balanced accuracy "
            f"{ai_bundle['metrics']['balanced_accuracy']*100:.1f}%. "
            "Targets: next 3 completed 5M candles within the same session; deadband is max(0.25%, 0.5× ATR%). RF vote fractions are uncalibrated model estimates, not true odds or a guaranteed win rate."
        )
        result = _apply_real_ai_brain(result)
        st.session_state["multi_agent_results"] = result
        # Refresh both downloads so the newly generated AI Brain fields are included.
        st.session_state["multi_agent_csv"] = result.to_csv(index=False).encode("utf-8-sig")
        try:
            from openpyxl import load_workbook
            existing_xlsx = st.session_state.get("multi_agent_xlsx")
            ai_export_signature = (
                st.session_state.get("ma_scan_job_id"),
                ai_bundle.get("metrics", {}).get("trained_at"),
                len(result),
            )
            if existing_xlsx and st.session_state.get("ma_ai_export_signature") != ai_export_signature:
                wb_ai = load_workbook(io.BytesIO(existing_xlsx))
                if "AI Scanner" in wb_ai.sheetnames:
                    ws_ai = wb_ai["AI Scanner"]
                    header_map = {str(cell.value): cell.column for cell in ws_ai[1]}
                    ai_export_cols = [
                        "AI Direction", "AI Up Probability %", "AI Down Probability %",
                        "AI Sideways Probability %", "AI Brain Signal", "AI Model Status",
                    ]
                    for col_name in ai_export_cols:
                        if col_name not in header_map:
                            new_col = ws_ai.max_column + 1
                            ws_ai.cell(1, new_col, col_name)
                            header_map[col_name] = new_col
                    symbol_col = header_map.get("Symbol", 1)
                    row_lookup = {
                        str(ws_ai.cell(r, symbol_col).value): r
                        for r in range(2, ws_ai.max_row + 1)
                    }
                    for _, ai_row in result.iterrows():
                        target_row = row_lookup.get(str(ai_row.get("Symbol", "")))
                        if not target_row:
                            continue
                        for col_name in ai_export_cols:
                            ws_ai.cell(target_row, header_map[col_name],
                                       None if pd.isna(ai_row.get(col_name)) else ai_row.get(col_name))
                    out_buffer = io.BytesIO()
                    wb_ai.save(out_buffer)
                    st.session_state["multi_agent_xlsx"] = out_buffer.getvalue()
                    st.session_state["ma_ai_export_signature"] = ai_export_signature
        except Exception as ai_export_exc:
            st.session_state["multi_agent_xlsx_error"] = f"AI export update: {ai_export_exc}"
        ai_cols = ["Symbol", "AI Direction", "AI Up Probability %", "AI Down Probability %",
                   "AI Sideways Probability %", "AI Brain Signal", "Final Signal"]
        st.dataframe(result[ai_cols].head(30), hide_index=True, **_stretch())
    else:
        # Ensure the report schema is stable even before 500 labelled samples exist.
        result = _apply_real_ai_brain(result)
        st.session_state["multi_agent_results"] = result
        st.session_state["multi_agent_csv"] = result.to_csv(index=False).encode("utf-8-sig")
        st.warning(
            "Real AI Brain is not active: " +
            str(st.session_state.get("ma_real_ai_train_error") or "training data not available") +
            " Collect more completed 5M candle history and press TRAIN / REFRESH."
        )

    # 18th Agent report: rank only qualified, actionable stocks.
    st.subheader("🏆 BEST PERFORMANCE STOCKS — 18th Ranking Analyst")
    min_best_score = st.selectbox(
        "Minimum Best Stock Score", [75, 80, 85, 90], index=0, key="ma_best_min_score"
    )
    best_cols = [
        "Symbol", "Best Stock Side", "Final Signal", "Action Signal", "Breakout Signal", "Best Stock Score",
        "Best Stock Grade", "Entry Price", "Stop Loss", "Last Price",
        "Signal Time (IST)", "5M", "15M", "1H", "Depth Signal",
        "Best Stock Reason",
    ]
    best = result[
        (result["Best Stock Filter"] == "PASS")
        & (result["Best Stock Score"] >= int(min_best_score))
    ].copy()
    if not best.empty:
        best = best.sort_values(
            ["Best Stock Score", "Confirmation Strength %"], ascending=[False, False]
        ).head(10)
        st.dataframe(best[best_cols], hide_index=True, **_stretch())
        st.caption(f"Showing top {len(best)} qualified stocks. Score threshold: {min_best_score}/100.")
    else:
        st.info(
            f"No stock passed {min_best_score}/100 with aligned 5M, 15M and 1H signals. "
            "The analyst will not force a BUY/SELL recommendation."
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
            "20M Early Movement / Liquidity",
            [
                "ALL", "WATCH", "CONFIRMED BREAKOUT", "WAIT", "NO DATA",
                "BUY LIQUIDITY", "SELL LIQUIDITY", "SWEEP UP", "SWEEP DOWN",
                "BREAKOUT", "BREAKDOWN", "ACTION BUY", "ACTION SELL",
            ],
            key="ma_early_filter",
        )
    view = result if signal_filter == "ALL" else result[result["Final Signal"] == signal_filter]

    # Liquidity filters use the completed 15M candle as the common reference.
    # Buy-side liquidity is above prior highs; sell-side liquidity is below prior lows.
    # Sweep UP means buy-side liquidity was taken and price closed back below.
    # Sweep DOWN means sell-side liquidity was taken and price closed back above.
    if early_filter in ("WATCH", "CONFIRMED BREAKOUT", "WAIT", "NO DATA"):
        view = view[view["20M Status"] == early_filter]
    elif early_filter == "BUY LIQUIDITY":
        view = view[view["15M Buy-side Liquidity"].notna()]
    elif early_filter == "SELL LIQUIDITY":
        view = view[view["15M Sell-side Liquidity"].notna()]
    elif early_filter == "SWEEP UP":
        view = view[view["15M Upside Sweep"] == "YES"]
    elif early_filter == "SWEEP DOWN":
        view = view[view["15M Downside Sweep"] == "YES"]
    elif early_filter in ("BREAKOUT", "BREAKDOWN"):
        view = view[view["Breakout Signal"] == early_filter]
    elif early_filter == "ACTION BUY":
        view = view[view["Action Signal"] == "BUY"]
    elif early_filter == "ACTION SELL":
        view = view[view["Action Signal"] == "SELL"]

    view = view.sort_values(["20M Pre-Move Score", "Confirmation Strength %"], ascending=[False, False])
    summary_cols = [
        "Symbol", "Final Signal", "Action Signal", "Breakout Signal", "Multi-TF Breakout", "5M Breakout", "15M Breakout", "1H Breakout", "Breakout Retest", "Breakout Level", "Breakdown Level", "Breakout Reason", "Equal High Liquidity", "Equal Low Liquidity", "Direction Movement", "Signal Time (IST)",
        "5M Candle Close (IST)", "15M Candle Close (IST)", "1H Candle Close (IST)",
        "Entry Price", "Stop Loss", "15M ATR",
        "Confirmation Strength %", "Consensus Score",
        "Buy-side Liquidity", "Sell-side Liquidity", "Upside Sweep", "Downside Sweep", "Liquidity Sweep Detail",
        "5M Buy-side Liquidity", "5M Sell-side Liquidity", "5M Upside Sweep", "5M Downside Sweep",
        "15M Buy-side Liquidity", "15M Sell-side Liquidity", "15M Upside Sweep", "15M Downside Sweep",
        "1H Buy-side Liquidity", "1H Sell-side Liquidity", "1H Upside Sweep", "1H Downside Sweep",
        "AI Direction", "AI Up Probability %", "AI Down Probability %",
        "AI Sideways Probability %", "AI Brain Signal", "AI Model Status",
        "18th Agent", "Best Stock Score", "Best Stock Side", "Best Stock Grade",
        "Best Stock Filter", "Best Stock Reason",
        "20M Status", "20M Direction", "20M Pre-Move Score", "20M Range High", "20M Range Low",
        "20M Range %", "20M RVOL", "20M Pressure %", "20M Last Close", "20M Reason",
        "5M", "15M", "1H", "5M Score", "15M Score", "1H Score",
        "5M Candles", "15M Candles", "1H Candles", "Last Price", "15M RSI",
        "News Agent", "News Score", "News Reason", "Depth Status", "Depth Signal",
        "Best Bid", "Best Ask", "Spread", "Buy Qty (5L)", "Sell Qty (5L)",
        "Depth Imbalance %", "Depth Time (IST)", "Errors",
    ]
    st.subheader("🚀 EARLY BIG MOVE CATCHER — 5M Compression + Volume Build-up")
    st.caption(f"{int(result.get('Symbol', pd.Series(dtype=str)).nunique()):,} unique scanned symbols: 12 completed 5M candles in a tight range, rising volume and directional bias; breakout is confirmed only after a completed candle closes beyond the range with strong body and RVOL.")
    prebig_view = result[result["PRE-BIG Status"].isin([
        "PRE-BIG BULLISH WATCH", "PRE-BIG BEARISH WATCH",
        "BULLISH BREAKOUT CONFIRMED", "BEARISH BREAKDOWN CONFIRMED",
    ])].copy()
    if not prebig_view.empty:
        prebig_view = prebig_view.sort_values(["PRE-BIG Score", "PRE-BIG RVOL"], ascending=[False, False])
        prebig_cols = ["Symbol", "PRE-BIG Status", "PRE-BIG Direction", "PRE-BIG Score",
                       "PRE-BIG Candles", "PRE-BIG Range High", "PRE-BIG Range Low",
                       "PRE-BIG Range Width %", "PRE-BIG Volume Build %", "PRE-BIG RVOL",
                       "PRE-BIG Trigger", "Last Price", "5M", "15M", "1H", "PRE-BIG Reason"]
        st.dataframe(prebig_view[prebig_cols], hide_index=True, **_stretch())
    else:
        st.info("No 5M PRE-BIG setup currently meets the compression + volume + direction rules. This is preferable to forcing a signal.")

    summary_cols = [c for c in summary_cols if c in view.columns]
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
    st.caption("CSV/Excel include all 51 timeframe-agent outputs, the Real AI Brain fields and the 18th Best Stock Analyst report and each agent reason. 5M PRE-BIG fields are calculated in the core scan; 20M early-movement fields need the optional 1M scan.")
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

    with st.expander("17 Agents + Real AI Brain — logic and limitations"):
        st.write(
            "The first 17 agents are deterministic OHLCV rule engines on 5M, 15M and 1H candles; the partial final 15-minute 60M bar is excluded. "
            "The Real AI Brain is a separate supervised RandomForest model trained from saved 5M candles; "
            "it predicts the next 3-candle direction using a purged chronological holdout. "
            "Its class vote fractions are uncalibrated model estimates, not calibrated probabilities. "
            "The 18th Best Stock Analyst ranks actionable signals using timeframe alignment, "
            "agent agreement, volume, structure and optional FYERS depth. It is a transparent "
            "screening score, not a probability or an independent AI language model. News is a separate keyword-based "
            "headline context check, not a standalone trade signal. Chart Pattern uses OHLCV, "
            "not chart screenshots. Signals are screening outputs, not guaranteed entries."
        )
        st.dataframe(pd.DataFrame({"Agent": AGENT_NAMES}), hide_index=True, **_stretch())
