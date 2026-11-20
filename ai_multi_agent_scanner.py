"""
AI Multi-Agent Scanner for NSE AI PRO V18 — ADVANCED BIG MOVEMENT
Yahoo Finance batch edition — advanced movement + false-breakout audit + Live Dashboard.

- scanner.py only supplies the NSE / F&O symbol universe; this module combines both loaders without modifying scanner.py.
- Yahoo Finance supplies all OHLCV candles and technical-analysis data.
- FYERS is used ONLY for 5-level Market Depth on shortlisted BUY/SELL/PRE-BIG stocks.
- Data Quality Gate + 15M Market Regime context before the 17-agent decision.
- 17 OHLCV rule-based agents on completed 5M, 15M and 1H candles.
- Directional UP/DOWN early pre-move watch using completed 5M candles + 15M alignment.
- Separate 20-candle 1M compression / volume buildup / breakout detector.
- Advanced Big Movement module: compression, directional pressure, breakout failure risk, retest and continuation context.
- 5M PRE-BIG requires at least 13 completed candles in the current NSE session (12-bar base + completed trigger candle).
- AI model vote fractions are labelled MODEL ESTIMATES, never probabilities without calibrated out-of-sample trade backtesting.
- Optional Yahoo headline context for the strongest technical shortlist.
- Live Dashboard: one-minute display refresh, immutable signal events, and 1/5/10/15/30-minute Yahoo price snapshots.
- Compatible entry point: show_ai_multi_agent_scanner(client=None)

requirements.txt:
    yfinance>=0.2.50
    openpyxl>=3.1.0
    scikit-learn>=1.4.0  # optional at import; required to train the Real AI Brain
"""

import io
import json
import copy
import math
import os
import pickle
import re
import sqlite3
import threading
from contextlib import contextmanager
import time
import uuid
from pathlib import Path
from collections import OrderedDict
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
import streamlit as st

try:
    import yfinance as yf
except Exception:
    yf = None

IST = ZoneInfo("Asia/Kolkata")
SESSION_OPEN = pd.Timedelta(hours=9, minutes=15)
SESSION_CLOSE = pd.Timedelta(hours=15, minutes=30)
# NSE 2026 equity holidays. Update this set when NSE publishes each new calendar.
# Source: NSE India - Market Timings & Holidays (2026 Equities calendar).
NSE_HOLIDAYS = {
    "2026-01-15", "2026-01-26", "2026-03-03", "2026-03-26",
    "2026-03-31", "2026-04-03", "2026-04-14", "2026-05-01",
    "2026-05-28", "2026-06-26", "2026-09-14", "2026-10-02",
    "2026-10-20", "2026-11-10", "2026-11-24", "2026-12-25",
}

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


# Thread-safe TTL cache for background Yahoo workers. Streamlit's cache_data
# is intentionally not used from worker threads.
_TTL_CACHE = OrderedDict()
_TTL_CACHE_LOCK = threading.Lock()
_TTL_CACHE_MAX_ITEMS = 32


def _ttl_cache_get(key, ttl_seconds):
    now = time.time()
    with _TTL_CACHE_LOCK:
        item = _TTL_CACHE.get(key)
        if item is None:
            return None
        saved_at, value = item
        if now - saved_at >= ttl_seconds:
            _TTL_CACHE.pop(key, None)
            return None
        _TTL_CACHE.move_to_end(key)
        return value.copy(deep=True) if isinstance(value, pd.DataFrame) else copy.deepcopy(value)


def _ttl_cache_set(key, value):
    with _TTL_CACHE_LOCK:
        # DataFrame cache entries are not mutated by consumers; each read returns
        # an isolated deep copy. Avoid an extra full-frame copy while inserting.
        stored = value.copy(deep=False) if isinstance(value, pd.DataFrame) else copy.deepcopy(value)
        _TTL_CACHE[key] = (time.time(), stored)
        _TTL_CACHE.move_to_end(key)
        while len(_TTL_CACHE) > _TTL_CACHE_MAX_ITEMS:
            _TTL_CACHE.popitem(last=False)


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
                    # Durable event ledger for the Live Dashboard. A signal event is
                    # immutable; later price updates are stored separately as snapshots.
                    conn.execute("""CREATE TABLE IF NOT EXISTS signal_events (
                        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_key TEXT NOT NULL UNIQUE,
                        symbol TEXT NOT NULL,
                        yahoo_symbol TEXT,
                        signal_type TEXT NOT NULL,
                        direction TEXT,
                        signal_price REAL,
                        signal_time_ist TEXT NOT NULL,
                        created_at_ist TEXT NOT NULL,
                        current_price REAL,
                        current_price_time_ist TEXT,
                        data_status TEXT DEFAULT 'WAITING',
                        status TEXT DEFAULT 'ACTIVE',
                        stop_price REAL,
                        target_price REAL,
                        outcome TEXT DEFAULT 'PENDING',
                        outcome_time_ist TEXT,
                        risk_reward REAL DEFAULT 1.5
                    )""")
                    # Safe additive migration: old SQLite databases keep all existing rows.
                    signal_columns = {r[1] for r in conn.execute("PRAGMA table_info(signal_events)").fetchall()}
                    for col_name, col_type in (("stop_price", "REAL"), ("target_price", "REAL"),
                                               ("outcome", "TEXT DEFAULT 'PENDING'"),
                                               ("outcome_time_ist", "TEXT"), ("risk_reward", "REAL DEFAULT 1.5")):
                        if col_name not in signal_columns:
                            conn.execute(f"ALTER TABLE signal_events ADD COLUMN {col_name} {col_type}")
                    conn.execute("""CREATE TABLE IF NOT EXISTS signal_price_snapshots (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_id INTEGER NOT NULL,
                        elapsed_minutes INTEGER NOT NULL,
                        price REAL NOT NULL,
                        price_time_ist TEXT NOT NULL,
                        change_pct REAL,
                        FOREIGN KEY(event_id) REFERENCES signal_events(event_id),
                        UNIQUE(event_id, elapsed_minutes)
                    )""")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_signal_events_created ON signal_events(created_at_ist)")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_signal_events_status ON signal_events(status, symbol)")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_signal_snapshots_event ON signal_price_snapshots(event_id)")
                    # Separate immutable ledger for liquidity levels and confirmed sweeps.
                    # Kept separate from signal_events so existing Active/Fresh counters do not change.
                    conn.execute("""CREATE TABLE IF NOT EXISTS liquidity_sweep_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_key TEXT NOT NULL UNIQUE,
                        symbol TEXT NOT NULL,
                        timeframe TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        level REAL,
                        direction TEXT,
                        signal_time_ist TEXT NOT NULL,
                        captured_at_ist TEXT NOT NULL,
                        candle_volume REAL,
                        average_volume REAL,
                        rvol REAL
                    )""")
                    liq_columns = {r[1] for r in conn.execute("PRAGMA table_info(liquidity_sweep_events)").fetchall()}
                    for col_name in ("candle_volume", "average_volume", "rvol"):
                        if col_name not in liq_columns:
                            conn.execute(f"ALTER TABLE liquidity_sweep_events ADD COLUMN {col_name} REAL")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_liquidity_events_time ON liquidity_sweep_events(signal_time_ist)")
                    conn.execute("""CREATE TABLE IF NOT EXISTS volume_expansion_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_key TEXT NOT NULL UNIQUE,
                        symbol TEXT NOT NULL,
                        timeframe TEXT NOT NULL,
                        direction TEXT NOT NULL,
                        candle_volume REAL,
                        average_volume REAL,
                        rvol REAL,
                        last_price REAL,
                        signal_time_ist TEXT NOT NULL,
                        captured_at_ist TEXT NOT NULL
                    )""")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_volume_events_time ON volume_expansion_events(signal_time_ist)")
                    # One-time cleanup of legacy partial 15:15 1H bars saved by older versions.
                    conn.execute("DELETE FROM candles WHERE timeframe='1H' AND substr(candle_time,12,5)='15:15'")
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


# Live Dashboard configuration. Dashboard refresh is separate from a full market scan.
LIVE_DASHBOARD_REFRESH_SECONDS = 60
LIVE_TRACKING_MINUTES = (1, 5, 10, 15, 30)
LIVE_EVENT_MAX_AGE_MINUTES = 180
LIVE_YAHOO_BATCH_SIZE = 50
_LIVE_PRICE_REFRESH_LOCK = threading.Lock()
_LIVE_PRICE_LAST_REFRESH_MONOTONIC = 0.0


def _as_ist_timestamp(value):
    """Parse timestamps safely and return a timezone-aware IST Timestamp."""
    try:
        if isinstance(value, str):
            value = re.sub(r"\s*IST\s*$", "", value.strip())
        ts = pd.Timestamp(value)
        if pd.isna(ts):
            return None
        if ts.tzinfo is None:
            return ts.tz_localize(IST)
        return ts.tz_convert(IST)
    except Exception:
        return None


SIGNAL_MAX_AGE_MINUTES = 45


def _is_fresh_signal_time(stamp, now=None):
    if stamp is None:
        return False
    now = _as_ist_timestamp(now) if now is not None else pd.Timestamp.now(tz=IST)
    if now is None:
        now = pd.Timestamp.now(tz=IST)
    age = (now - stamp).total_seconds() / 60.0
    return -2.0 <= age <= SIGNAL_MAX_AGE_MINUTES


def _risk_levels_for_event(row, direction, entry):
    """Return stop/target using the existing scanner stop when valid, else 1.5 x 15M ATR."""
    stop = _safe_num(row.get("Stop Loss"), np.nan)
    if not np.isfinite(stop) or stop <= 0 or (direction == "UP" and stop >= entry) or (direction == "DOWN" and stop <= entry):
        atr = _safe_num(row.get("15M ATR"), np.nan)
        if not np.isfinite(atr) or atr <= 0:
            return None, None
        stop = entry - 1.5 * atr if direction == "UP" else entry + 1.5 * atr
    risk = abs(entry - stop)
    if risk <= 0:
        return None, None
    target = entry + 1.5 * risk if direction == "UP" else entry - 1.5 * risk
    return float(stop), float(target)


def _prune_event_ledgers(signal_days=90, liquidity_days=14):
    """Bound local event-ledger growth while retaining recent accuracy history."""
    now = pd.Timestamp.now(tz=IST)
    sig_cut = (now - pd.Timedelta(days=signal_days)).isoformat(timespec="seconds")
    liq_cut = (now - pd.Timedelta(days=liquidity_days)).isoformat(timespec="seconds")
    with _memory_connect() as conn:
        conn.execute("""DELETE FROM signal_price_snapshots WHERE event_id IN
                        (SELECT event_id FROM signal_events WHERE created_at_ist<?)""", (sig_cut,))
        conn.execute("DELETE FROM signal_events WHERE created_at_ist<?", (sig_cut,))
        conn.execute("DELETE FROM liquidity_sweep_events WHERE captured_at_ist<?", (liq_cut,))
        conn.execute("DELETE FROM volume_expansion_events WHERE captured_at_ist<?", (liq_cut,))


def _drop_recent_duplicate_events(conn, rows, minutes=60):
    """Drop same-symbol/same-signal-type events already logged recently or repeated in this batch."""
    cut = (pd.Timestamp.now(tz=IST) - pd.Timedelta(minutes=minutes)).isoformat(timespec="seconds")
    seen = {(r[0], r[1]) for r in conn.execute(
        "SELECT symbol, signal_type FROM signal_events WHERE created_at_ist>=?", (cut,)
    )}
    kept = []
    # Newest signal candle first, so the freshest event wins inside a batch.
    for r in sorted(rows, key=lambda x: x[6], reverse=True):
        key = (r[1], r[3])
        if key in seen:
            continue
        seen.add(key)
        kept.append(r)
    return kept


def _capture_signal_events(result, reference_time=None):
    """Persist BUY/SELL/PRE-BIG/confirmed breakout signals with fixed 1:1.5 outcome levels."""
    if not isinstance(result, pd.DataFrame) or result.empty:
        return 0
    now_text = datetime.now(IST).isoformat(timespec="seconds")
    rows = []
    actionable = {"BUY", "SELL", "PRE-BIG BUY", "PRE-BIG SELL"}
    for _, row in result.iterrows():
        if str(row.get("Data Quality", "PASS")).strip().upper() != "PASS":
            continue
        symbol = str(row.get("Symbol", "")).strip()
        if not symbol:
            continue
        final_signal = str(row.get("Final Signal", "WAIT") or "WAIT").strip().upper()
        breakout = str(row.get("Breakout Signal", "WAIT") or "WAIT").strip().upper()
        advanced = str(row.get("Advanced Big Move", "") or "").strip().upper()
        signal_type, direction = None, None
        if advanced in ("BREAKOUT DETECTED", "BREAKDOWN DETECTED", "RETEST WAITING",
                        "RETEST HELD — 15M WAIT", "RETEST REJECTED — 15M WAIT",
                        "FALSE BREAKOUT RISK"):
            continue
        if breakout in ("BREAKOUT", "BREAKDOWN") and advanced not in ("BUY CONFIRMED", "SELL CONFIRMED"):
            continue
        if final_signal in actionable:
            direction = "UP" if "BUY" in final_signal else "DOWN"
            if breakout in ("BREAKOUT", "BREAKDOWN"):
                signal_type = f"{final_signal} / CONFIRMED {breakout}"
            elif advanced in ("BUY CONFIRMED", "SELL CONFIRMED"):
                signal_type = advanced
            else:
                signal_type = final_signal
        elif breakout in ("BREAKOUT", "BREAKDOWN"):
            signal_type = f"CONFIRMED {breakout}"
            direction = "UP" if breakout == "BREAKOUT" else "DOWN"
        elif advanced in ("BUY CONFIRMED", "SELL CONFIRMED"):
            signal_type = advanced
            direction = "UP" if advanced == "BUY CONFIRMED" else "DOWN"
        if not signal_type:
            continue
        candle_time = _as_ist_timestamp(row.get("Signal Time (IST)"))
        if candle_time is None or not _is_fresh_signal_time(candle_time, now=reference_time):
            continue
        price = _safe_num(row.get("Entry Price"), 0.0)
        if price <= 0:
            price = _safe_num(row.get("Last Price"), 0.0)
        if price <= 0:
            continue
        stop, target = _risk_levels_for_event(row, direction, price)
        yahoo_symbol = str(row.get("Yahoo Symbol", "") or "").strip()
        candle_time_text = candle_time.isoformat(timespec="seconds")
        event_key = f"{symbol}|{signal_type}|{candle_time_text}"
        rows.append((event_key, symbol, yahoo_symbol, signal_type, direction, float(price),
                     candle_time_text, now_text, now_text, stop, target))
    if not rows:
        return 0
    with _memory_connect() as conn:
        rows = _drop_recent_duplicate_events(conn, rows)
        if not rows:
            return 0
        before = conn.total_changes
        conn.executemany("""INSERT OR IGNORE INTO signal_events
            (event_key,symbol,yahoo_symbol,signal_type,direction,signal_price,
             signal_time_ist,created_at_ist,current_price_time_ist,stop_price,target_price,
             risk_reward,data_status,status,outcome)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,1.5,'WAITING','ACTIVE','PENDING')""", rows)
        return conn.total_changes - before


def _capture_liquidity_sweep_events(result, reference_time=None):
    """Persist liquidity pivot times and confirmed sweep candle times without changing signal counters."""
    if not isinstance(result, pd.DataFrame) or result.empty:
        return 0
    now_text = datetime.now(IST).isoformat(timespec="seconds")
    rows = []
    frames = [
        ("5M", "5M Buy-side Liquidity", "5M Buy-side Liquidity Time (IST)",
         "5M Sell-side Liquidity", "5M Sell-side Liquidity Time (IST)",
         "5M Upside Sweep", "5M Upside Sweep Time (IST)",
         "5M Downside Sweep", "5M Downside Sweep Time (IST)"),
        ("15M", "15M Buy-side Liquidity", "15M Buy-side Liquidity Time (IST)",
         "15M Sell-side Liquidity", "15M Sell-side Liquidity Time (IST)",
         "15M Upside Sweep", "15M Upside Sweep Time (IST)",
         "15M Downside Sweep", "15M Downside Sweep Time (IST)"),
        ("1H", "1H Buy-side Liquidity", "1H Buy-side Liquidity Time (IST)",
         "1H Sell-side Liquidity", "1H Sell-side Liquidity Time (IST)",
         "1H Upside Sweep", "1H Upside Sweep Time (IST)",
         "1H Downside Sweep", "1H Downside Sweep Time (IST)"),
    ]
    for _, row in result.iterrows():
        if str(row.get("Data Quality", "PASS")).strip().upper() != "PASS":
            continue
        symbol = str(row.get("Symbol", "") or "").strip()
        if not symbol:
            continue
        for tf, buy_col, buy_time_col, sell_col, sell_time_col, up_col, up_time_col, down_col, down_time_col in frames:
            # The unprefixed fields are the scanner's primary 15M view.
            if tf == "15M":
                buy_col, buy_time_col = "Buy-side Liquidity", "Buy-side Liquidity Time (IST)"
                sell_col, sell_time_col = "Sell-side Liquidity", "Sell-side Liquidity Time (IST)"
                up_col, up_time_col = "Upside Sweep", "Upside Sweep Time (IST)"
                down_col, down_time_col = "Downside Sweep", "Downside Sweep Time (IST)"
            for event_type, level_col, time_col, direction in (
                ("BUY LIQUIDITY", buy_col, buy_time_col, "BUY"),
                ("SELL LIQUIDITY", sell_col, sell_time_col, "SELL"),
                ("SWEEP UP", None, up_time_col, "SELL"),
                ("SWEEP DOWN", None, down_time_col, "BUY"),
            ):
                if event_type == "SWEEP UP" and str(row.get(up_col, "NO")).upper() != "YES":
                    continue
                if event_type == "SWEEP DOWN" and str(row.get(down_col, "NO")).upper() != "YES":
                    continue
                stamp = _as_ist_timestamp(row.get(time_col))
                if stamp is None:
                    continue
                level = _safe_num(row.get(level_col), np.nan) if level_col else np.nan
                if event_type.endswith("LIQUIDITY") and not np.isfinite(level):
                    continue
                stamp_text = stamp.isoformat(timespec="seconds")
                event_key = f"{symbol}|{tf}|{event_type}|{stamp_text}|{level if np.isfinite(level) else ''}"
                rows.append((event_key, symbol, tf, event_type,
                             float(level) if np.isfinite(level) else None,
                             direction, stamp_text, now_text,
                             _safe_num(row.get(f"{tf} Candle Volume"), 0.0),
                             _safe_num(row.get(f"{tf} Average Volume 20"), 0.0),
                             _safe_num(row.get(f"{tf} RVOL"), 0.0)))
    if not rows:
        return 0
    with _memory_connect() as conn:
        before = conn.total_changes
        conn.executemany("""INSERT OR IGNORE INTO liquidity_sweep_events
            (event_key,symbol,timeframe,event_type,level,direction,signal_time_ist,captured_at_ist,candle_volume,average_volume,rvol)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""", rows)
        inserted = conn.total_changes - before
        # Promote confirmed 15M sweep events into the same live accuracy ledger.
        sweep_rows = []
        for _, row in result.iterrows():
            if str(row.get("Data Quality", "PASS")).strip().upper() != "PASS":
                continue
            symbol = str(row.get("Symbol", "") or "").strip()
            if not symbol:
                continue
            for sweep_name, flag_col, time_col, direction in (
                ("SWEEP UP", "Upside Sweep", "Upside Sweep Time (IST)", "DOWN"),
                ("SWEEP DOWN", "Downside Sweep", "Downside Sweep Time (IST)", "UP"),
            ):
                if str(row.get(flag_col, "NO")).upper() != "YES":
                    continue
                stamp = _as_ist_timestamp(row.get(time_col))
                entry = _safe_num(row.get("Last Price"), 0.0)
                if stamp is None or entry <= 0 or not _is_fresh_signal_time(stamp, now=reference_time):
                    continue
                stop, target = _risk_levels_for_event(row, direction, entry)
                stamp_text = stamp.isoformat(timespec="seconds")
                event_key = f"{symbol}|{sweep_name}|{stamp_text}"
                sweep_rows.append((event_key, symbol, str(row.get("Yahoo Symbol", "") or "").strip(),
                    sweep_name, direction, entry, stamp_text, now_text, now_text, stop, target))
        sweep_rows = _drop_recent_duplicate_events(conn, sweep_rows)
        if sweep_rows:
            conn.executemany("""INSERT OR IGNORE INTO signal_events
                (event_key,symbol,yahoo_symbol,signal_type,direction,signal_price,
                 signal_time_ist,created_at_ist,current_price_time_ist,stop_price,target_price,
                 risk_reward,data_status,status,outcome)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,1.5,'WAITING','ACTIVE','PENDING')""", sweep_rows)
            inserted += conn.total_changes - before - inserted
        return inserted


def _capture_volume_expansion_events(result, threshold=2.0, reference_time=None):
    """Persist completed-candle volume expansion events at >=2x prior 20-bar average."""
    if not isinstance(result, pd.DataFrame) or result.empty:
        return 0
    now_text = datetime.now(IST).isoformat(timespec="seconds")
    rows = []
    for _, row in result.iterrows():
        if str(row.get("Data Quality", "PASS")).strip().upper() != "PASS":
            continue
        symbol = str(row.get("Symbol", "") or "").strip()
        if not symbol:
            continue
        for tf in TIMEFRAMES:
            rvol = _safe_num(row.get(f"{tf} RVOL"), 0.0)
            direction = str(row.get(f"{tf} Candle Direction", "WAIT")).upper()
            volume = _safe_num(row.get(f"{tf} Candle Volume"), 0.0)
            average = _safe_num(row.get(f"{tf} Average Volume 20"), 0.0)
            price = _safe_num(row.get("Last Price"), 0.0)
            # Use the actual completed candle close time for this timeframe.
            stamp = _as_ist_timestamp(row.get(f"{tf} Candle Close (IST)"))
            if stamp is None:
                stamp = _as_ist_timestamp(row.get("Signal Time (IST)"))
            if rvol < threshold or direction not in ("BUY", "SELL") or volume <= 0 or average <= 0 or stamp is None:
                continue
            stamp_text = stamp.isoformat(timespec="seconds")
            event_key = f"{symbol}|{tf}|{direction}|{stamp_text}"
            rows.append((event_key, symbol, tf, direction, volume, average, rvol,
                         price if price > 0 else None, stamp_text, now_text))
    if not rows:
        return 0
    with _memory_connect() as conn:
        before = conn.total_changes
        conn.executemany("""INSERT OR IGNORE INTO volume_expansion_events
            (event_key,symbol,timeframe,direction,candle_volume,average_volume,rvol,last_price,signal_time_ist,captured_at_ist)
            VALUES (?,?,?,?,?,?,?,?,?,?)""", rows)
        return conn.total_changes - before


def _load_volume_expansion_events(limit=500):
    try:
        with _memory_connect() as conn:
            return pd.read_sql_query("""SELECT symbol,timeframe,direction,candle_volume,average_volume,rvol,last_price,
                signal_time_ist,captured_at_ist FROM volume_expansion_events
                ORDER BY signal_time_ist DESC LIMIT ?""", conn, params=(int(limit),))
    except Exception:
        return pd.DataFrame()


def _load_liquidity_sweep_events(limit=500):
    try:
        with _memory_connect() as conn:
            return pd.read_sql_query("""SELECT symbol,timeframe,event_type,level,direction,candle_volume,average_volume,rvol,
                signal_time_ist,captured_at_ist FROM liquidity_sweep_events
                ORDER BY signal_time_ist DESC LIMIT ?""", conn, params=(int(limit),))
    except Exception:
        return pd.DataFrame()


def _render_live_liquidity_sweep_panel():
    """Show persisted liquidity/sweep event times on the existing Live Market Dashboard."""
    st.markdown("#### 💧 BUY / SELL LIQUIDITY + SWEEP SIGNAL TIMES (IST)")
    liq = _load_liquidity_sweep_events()
    if liq.empty:
        st.info("Liquidity/Sweep timestamps ఇంకా save కాలేదు. Scannerను కనీసం ఒకసారి run చేసిన తర్వాత ఇక్కడ కనిపిస్తాయి.")
        return
    liq = liq.copy()
    liq["Signal Time (IST)"] = pd.to_datetime(liq["signal_time_ist"], errors="coerce", utc=True).dt.tz_convert(IST).dt.strftime("%Y-%m-%d %H:%M:%S IST")
    liq["Liquidity Level"] = liq["level"].map(lambda x: f"{x:.2f}" if pd.notna(x) else "—")
    liq = liq.rename(columns={"symbol":"Stock", "timeframe":"Timeframe", "event_type":"Signal", "direction":"Direction"})
    liq = liq.rename(columns={"candle_volume":"Candle Volume", "average_volume":"Average Volume (20)", "rvol":"RVOL"})
    view_cols = ["Stock", "Timeframe", "Signal", "Direction", "Liquidity Level", "Candle Volume", "Average Volume (20)", "RVOL", "Signal Time (IST)"]
    sweeps = liq[liq["Signal"].isin(["SWEEP UP", "SWEEP DOWN"])]
    levels = liq[liq["Signal"].isin(["BUY LIQUIDITY", "SELL LIQUIDITY"])]
    st.markdown("**Confirmed Sweep Signals**")
    if sweeps.empty:
        st.caption("ఇప్పటివరకు confirmed Sweep Up / Sweep Down event లేదు.")
    else:
        st.dataframe(sweeps[view_cols].head(100), hide_index=True, **_stretch())
    st.markdown("**BUY / SELL Liquidity Levels**")
    st.dataframe(levels[view_cols].head(150), hide_index=True, **_stretch())
    st.caption("Liquidity Time = pivot candle close time • Sweep Time = sweep confirm అయిన completed candle close time • Times are IST.")


def _load_live_signal_events(limit=10000):
    """Load recent event ledger rows and their fixed-interval snapshots."""
    try:
        with _memory_connect() as conn:
            events = pd.read_sql_query("""SELECT event_id,event_key,symbol,yahoo_symbol,
                signal_type,direction,signal_price,signal_time_ist,created_at_ist,
                current_price,current_price_time_ist,data_status,status,stop_price,target_price,
                outcome,outcome_time_ist,risk_reward
                FROM signal_events ORDER BY created_at_ist DESC LIMIT ?""", conn, params=(int(limit),))
            snapshots = pd.read_sql_query("""SELECT event_id,elapsed_minutes,price,
                price_time_ist,change_pct FROM signal_price_snapshots
                ORDER BY event_id,elapsed_minutes""", conn)
        return events, snapshots
    except Exception:
        return pd.DataFrame(), pd.DataFrame()


def _refresh_live_signal_prices():
    """Refresh only symbols with active signal events; never launches a full NSE scan."""
    global _LIVE_PRICE_LAST_REFRESH_MONOTONIC
    if yf is None or not _is_market_hours():
        return {"updated": 0, "error": None, "symbols": 0, "skipped": True}
    with _LIVE_PRICE_REFRESH_LOCK:
        monotonic_now = time.monotonic()
        if monotonic_now - _LIVE_PRICE_LAST_REFRESH_MONOTONIC < 55:
            return {"updated": 0, "error": None, "symbols": 0, "skipped": True}
        _LIVE_PRICE_LAST_REFRESH_MONOTONIC = monotonic_now
    now = datetime.now(IST)
    cutoff = (now - pd.Timedelta(minutes=LIVE_EVENT_MAX_AGE_MINUTES)).isoformat(timespec="seconds")
    with _memory_connect() as conn:
        active = pd.read_sql_query("""SELECT event_id,symbol,yahoo_symbol,signal_price,signal_time_ist,created_at_ist,direction,stop_price,target_price
            FROM signal_events WHERE status='ACTIVE' AND created_at_ist>=?
            ORDER BY created_at_ist DESC""", conn, params=(cutoff,))
        conn.execute("UPDATE signal_events SET status='EXPIRED' WHERE status='ACTIVE' AND created_at_ist<?", (cutoff,))
    if active.empty:
        return {"updated": 0, "error": None, "symbols": 0}

    ticker_map = {}
    for _, item in active.iterrows():
        ticker = str(item.get("yahoo_symbol") or "").strip()
        if not ticker:
            ticker = _to_yahoo_symbol(str(item.get("symbol", "")))
        if ticker:
            ticker_map[ticker] = str(item.get("symbol", ""))
    tickers = list(ticker_map)
    if not tickers:
        return {"updated": 0, "error": "No valid Yahoo symbols in active events.", "symbols": 0}

    updated = 0
    try:
        # Yahoo 1-minute history is used only for the event-ledger symbols.
        # Chunking limits oversized requests; provider throttling can still occur.
        for start in range(0, len(tickers), LIVE_YAHOO_BATCH_SIZE):
            batch = tuple(tickers[start:start + LIVE_YAHOO_BATCH_SIZE])
            with _YF_LOCK:
                data = yf.download(
                    tickers=list(batch), period="1d", interval="1m",
                    auto_adjust=False, actions=False, progress=False,
                    threads=False, group_by="ticker", timeout=20,
                )
            if data is None or data.empty:
                continue
            for ticker in batch:
                frame = _extract_one_ticker(data, ticker)
                if frame is None or frame.empty or "Close" not in frame.columns:
                    continue
                close = pd.to_numeric(frame["Close"], errors="coerce")
                if close.dropna().empty:
                    continue
                index = pd.to_datetime(frame.index, errors="coerce")
                if getattr(index, "tz", None) is None:
                    index = index.tz_localize(IST)
                else:
                    index = index.tz_convert(IST)
                prices = pd.DataFrame({"time": index, "price": close.to_numpy(),
                    "high": pd.to_numeric(frame["High"], errors="coerce").to_numpy() if "High" in frame else close.to_numpy(),
                    "low": pd.to_numeric(frame["Low"], errors="coerce").to_numpy() if "Low" in frame else close.to_numpy()}).dropna(subset=["time", "price"])
                prices = prices[prices["price"] > 0].sort_values("time")
                if prices.empty:
                    continue
                latest = prices.iloc[-1]
                latest_time = pd.Timestamp(latest["time"]).isoformat(timespec="seconds")
                latest_price = float(latest["price"])
                ticker_events = active[active["yahoo_symbol"].astype(str) == ticker]
                if ticker_events.empty:
                    ticker_events = active[active["symbol"].astype(str) == ticker_map.get(ticker, "")]

                with _memory_connect() as conn:
                    for _, event in ticker_events.iterrows():
                        event_id = int(event["event_id"])
                        signal_price = _safe_num(event.get("signal_price"), 0.0)
                        conn.execute("""UPDATE signal_events SET current_price=?,
                            current_price_time_ist=?,data_status='YAHOO 1M' WHERE event_id=?""",
                            (latest_price, latest_time, event_id))
                        updated += 1
                        # Resolve target/stop from 1M high/low after signal time.
                        stop_price = _safe_num(event.get("stop_price"), np.nan)
                        target_price = _safe_num(event.get("target_price"), np.nan)
                        direction = str(event.get("direction", "")).upper()
                        created = _as_ist_timestamp(event.get("created_at_ist"))
                        signal_stamp = _as_ist_timestamp(event.get("signal_time_ist"))
                        if signal_stamp is not None and np.isfinite(stop_price) and np.isfinite(target_price):
                            # Evaluate every completed 1M bar after the signal candle,
                            # including bars before the dashboard captured the event.
                            anchor = signal_stamp
                            future_bars = prices[prices["time"] >= anchor]
                            resolved = None
                            resolved_time = None
                            for _, bar in future_bars.iterrows():
                                hi, lo = float(bar["high"]), float(bar["low"])
                                if direction == "UP":
                                    hit_stop, hit_target = lo <= stop_price, hi >= target_price
                                else:
                                    hit_stop, hit_target = hi >= stop_price, lo <= target_price
                                if hit_stop or hit_target:
                                    # If both levels fall inside one 1M candle, count LOSS conservatively.
                                    resolved = "LOSS" if hit_stop else "WIN"
                                    resolved_time = pd.Timestamp(bar["time"]).isoformat(timespec="seconds")
                                    break
                            if resolved and created is not None and resolved_time is not None:
                                resolved_stamp = _as_ist_timestamp(resolved_time)
                                if resolved_stamp is not None and resolved_stamp <= created:
                                    resolved = "MISSED"  # target/stop already reached before event capture
                            if resolved:
                                conn.execute("UPDATE signal_events SET outcome=?,status=?,outcome_time_ist=? WHERE event_id=? AND outcome='PENDING'",
                                    (resolved, resolved, resolved_time, event_id))
                        if created is None:
                            continue
                        for target in LIVE_TRACKING_MINUTES:
                            target_time = created + pd.Timedelta(minutes=target)
                            if target_time > pd.Timestamp(latest["time"]):
                                continue  # checkpoint has not been reached yet
                            diffs = (prices["time"] - target_time).abs()
                            nearest_idx = diffs.idxmin()
                            if pd.isna(nearest_idx) or float(diffs.loc[nearest_idx].total_seconds()) > 120:
                                continue
                            snap = prices.loc[nearest_idx]
                            snap_price = float(snap["price"])
                            snap_time = pd.Timestamp(snap["time"]).isoformat(timespec="seconds")
                            change_pct = ((snap_price / signal_price) - 1.0) * 100.0 if signal_price > 0 else None
                            conn.execute("""INSERT OR IGNORE INTO signal_price_snapshots
                                (event_id,elapsed_minutes,price,price_time_ist,change_pct)
                                VALUES (?,?,?,?,?)""",
                                (event_id, int(target), snap_price, snap_time, change_pct))
        return {"updated": updated, "error": None, "symbols": len(tickers)}
    except Exception as exc:
        return {"updated": updated, "error": str(exc)[:300], "symbols": len(tickers)}


def _render_live_volume_expansion_panel():
    """Display completed 5M/15M/1H candles with volume >= 2x prior 20-bar average."""
    st.markdown("#### 🔊 LIVE VOLUME INCREASE STOCKS — RVOL ≥ 2.0x")
    vol = _load_volume_expansion_events()
    if vol.empty:
        st.info("2x లేదా అంతకంటే ఎక్కువ Relative Volume ఉన్న completed-candle signals ఇంకా save కాలేదు. Fresh scan run చేయండి.")
        return
    vol = vol.copy()
    vol["Signal Time (IST)"] = pd.to_datetime(vol["signal_time_ist"], errors="coerce", utc=True).dt.tz_convert(IST).dt.strftime("%Y-%m-%d %H:%M:%S IST")
    vol = vol.rename(columns={"symbol":"Stock", "timeframe":"Timeframe", "direction":"Candle Direction",
        "candle_volume":"Candle Volume", "average_volume":"Average Volume (20)",
        "rvol":"RVOL", "last_price":"LTP"})
    vol["Volume Signal"] = np.where(vol["Candle Direction"].eq("BUY"), "BUY VOLUME WATCH", "SELL VOLUME WATCH")
    cols = ["Stock", "Timeframe", "LTP", "Candle Direction", "Candle Volume", "Average Volume (20)", "RVOL", "Volume Signal", "Signal Time (IST)"]
    st.dataframe(vol[cols].head(150), hide_index=True, **_stretch())
    st.caption("Threshold: current completed candle volume ≥ 2 × average volume of previous 20 completed candles. Direction is candle body only; it is a watch signal, not an automatic trade entry.")


def _render_live_dashboard_body():
    """Render dashboard using SQLite; price refresh is throttled by the fragment clock."""
    refresh_result = _refresh_live_signal_prices()
    events, snapshots = _load_live_signal_events()
    st.subheader("📡 LIVE MARKET DASHBOARD")
    st.caption("Dashboard refresh: 60 seconds • BUY / SELL / SWEEP + Volume ≥2x • 1:1.5 target tracking • Prices: Yahoo Finance 1M feed")
    if refresh_result.get("error"):
        st.warning(f"Yahoo price update issue: {refresh_result['error']}. Last saved prices are retained and labelled by age.")

    _render_live_stock_boards()
    if events.empty:
        cols = st.columns(4)
        for col, label in zip(cols, ("Active Signals", "Fresh Signals", "Breakout Events", "Tracked Stocks")):
            col.metric(label, "0")
        st.info("No signal events saved yet. Run the V18 scanner once; newly detected signals will appear here and remain in SQLite.")
        st.divider()
        return

    now = pd.Timestamp.now(tz=IST)
    events["_created"] = pd.to_datetime(events["created_at_ist"], errors="coerce", utc=True).dt.tz_convert(IST)
    events["_age_min"] = (now - events["_created"]).dt.total_seconds() / 60.0
    events["_age_min"] = events["_age_min"].clip(lower=0)
    events["Signal Age"] = events["_age_min"].map(lambda x: f"{int(x)} min" if pd.notna(x) else "UNKNOWN")
    _raw_chg = np.where(
        pd.to_numeric(events["signal_price"], errors="coerce") > 0,
        (pd.to_numeric(events["current_price"], errors="coerce") /
         pd.to_numeric(events["signal_price"], errors="coerce") - 1.0) * 100.0,
        np.nan,
    )
    _sign = np.where(events["direction"].astype(str).str.upper() == "DOWN", -1.0, 1.0)
    events["Price Change %"] = _raw_chg * _sign  # positive = in favour of the signal
    update_times = pd.to_datetime(events["current_price_time_ist"], errors="coerce", utc=True).dt.tz_convert(IST)
    update_age = (now - update_times).dt.total_seconds() / 60.0
    events["Price Data Status"] = np.where(
        update_times.isna() | pd.to_numeric(events["current_price"], errors="coerce").isna(), "NO DATA",
        np.where(update_age <= 3, "YAHOO UPDATED", np.where(update_age <= 10, "DELAYED", "STALE")),
    )
    # Historical accuracy uses only resolved WIN/LOSS outcomes; PENDING/EXPIRED are excluded.
    resolved = events[events["outcome"].isin(["WIN", "LOSS"])].copy()
    st.markdown("#### 🎯 LIVE SIGNAL ACCURACY — 1:1.5 RISK / REWARD")
    accuracy_types = ["BUY", "SELL", "SWEEP UP", "SWEEP DOWN"]
    acc_cols = st.columns(4)
    for col, label in zip(acc_cols, accuracy_types):
        subset = resolved[resolved["signal_type"].astype(str).str.upper().str.startswith(label)]
        count = len(subset)
        wins = int((subset["outcome"] == "WIN").sum())
        accuracy = (wins / count * 100.0) if count else 0.0
        col.metric(label, f"{accuracy:.1f}%" if count else "N/A", f"{wins}/{count} WIN")
    st.caption("Accuracy = WIN ÷ (WIN + LOSS) × 100. Pending, expired, MISSED (target or stop already reached before detection) or data-missing signals are not counted. Same 1-minute candle touching both Target and Stop Loss is conservatively counted as LOSS.")

    active = events[events["status"] == "ACTIVE"].copy()
    fresh = active[active["_age_min"] <= 5].copy()
    breakout_mask = events["signal_type"].astype(str).str.contains(
        "BREAKOUT|BREAKDOWN|BUY CONFIRMED|SELL CONFIRMED", case=False, regex=True
    )
    existing_breakouts = events[breakout_mask].copy()
    cols = st.columns(4)
    cols[0].metric("Active Signals", f"{len(active):,}")
    cols[1].metric("Fresh Signals ≤ 5m", f"{len(fresh):,}")
    cols[2].metric("Breakout / Breakdown Events", f"{len(existing_breakouts):,}")
    cols[3].metric("Tracked Stocks", f"{events['symbol'].nunique():,}")

    display_cols = ["symbol", "signal_type", "signal_price", "stop_price", "target_price",
                    "current_price", "Price Change %", "outcome", "created_at_ist",
                    "Signal Age", "Price Data Status", "status"]
    labels = {"symbol": "Stock", "signal_type": "Signal", "signal_price": "Entry",
              "stop_price": "Stop Loss", "target_price": "Target (1:1.5)",
              "current_price": "Current Price", "outcome": "Result",
              "created_at_ist": "Detected Time (IST)", "status": "Event Status"}
    st.markdown("#### 🔴 LIVE BUY / SELL / SWEEP SIGNALS")
    live_signal_rows = active[active["signal_type"].astype(str).str.upper().str.contains(
        r"^(BUY|SELL|SWEEP UP|SWEEP DOWN)", regex=True, na=False
    )].copy()
    if live_signal_rows.empty:
        st.caption("ప్రస్తుతం Active BUY / SELL / SWEEP UP / SWEEP DOWN signals లేవు.")
    else:
        st.dataframe(live_signal_rows[display_cols].head(150).rename(columns=labels),
                     hide_index=True, **_stretch())

    st.markdown("#### 🟢 FRESH SIGNALS")
    if fresh.empty:
        st.caption("No signal detected in the last 5 minutes.")
    else:
        st.dataframe(fresh[display_cols].rename(columns=labels).head(100), hide_index=True, **_stretch())

    st.markdown("#### ⚡ EXISTING BREAKOUT / BREAKDOWN STOCKS")
    if existing_breakouts.empty:
        st.caption("No confirmed breakout / breakdown event has been recorded yet.")
    else:
        st.dataframe(existing_breakouts[display_cols].rename(columns=labels).head(150), hide_index=True, **_stretch())

    st.markdown("#### 📈 ALL RECENT SIGNALS + PRICE TRACKING")
    recent = events.head(150).copy()
    if snapshots.empty:
        st.caption("Price snapshots will populate when Yahoo 1M candles become available at the 1/5/10/15/30-minute checkpoints.")
    else:
        snap_view = snapshots[snapshots["event_id"].isin(recent["event_id"])].copy()
        if not snap_view.empty:
            pivot = snap_view.pivot_table(index="event_id", columns="elapsed_minutes", values="price", aggfunc="last")
            pivot.columns = [f"Price +{int(c)}m" for c in pivot.columns]
            recent = recent.merge(pivot, left_on="event_id", right_index=True, how="left")
    track_cols = ["symbol", "signal_type", "signal_price", "stop_price", "target_price",
                  "current_price", "Price Change %", "outcome", "Signal Age", "Price Data Status"] + [c for c in recent.columns if c.startswith("Price +")]
    st.dataframe(recent[track_cols].rename(columns=labels), hide_index=True, **_stretch())

    with st.expander("Signal event detail — candle time and snapshot timestamps"):
        detail_cols = ["symbol", "signal_type", "signal_time_ist", "created_at_ist", "yahoo_symbol",
                       "stop_price", "target_price", "risk_reward", "outcome", "outcome_time_ist", "data_status", "status"]
        st.dataframe(events[detail_cols].head(100), hide_index=True, **_stretch())
        st.caption("Signal candle time is the completed 15M candle timestamp from V18. Detected Time is when this scanner run first recorded the event; 1/5/10/15/30-minute tracking is measured from Detected Time.")
    st.caption(f"Last dashboard refresh: {datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S IST')} • Updated event rows: {refresh_result.get('updated', 0)} • Yahoo symbols requested: {refresh_result.get('symbols', 0)}")
    st.divider()


def _render_live_dashboard():
    """One-minute Streamlit fragment; falls back to a normal render on older versions."""
    if hasattr(st, "fragment"):
        @st.fragment(run_every="60s")
        def _live_dashboard_fragment():
            _render_live_dashboard_body()
        _live_dashboard_fragment()
    else:
        st.info("Automatic 60-second dashboard refresh needs Streamlit fragments. Showing the latest saved dashboard data.")
        _render_live_dashboard_body()


def _is_market_hours(now=None):
    """NSE regular-session clock, excluding weekends and configured holidays."""
    now = now or datetime.now(IST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)
    else:
        now = now.astimezone(IST)
    if now.weekday() >= 5 or now.strftime("%Y-%m-%d") in NSE_HOLIDAYS:
        return False
    return (now.hour, now.minute) >= (9, 15) and (now.hour, now.minute) < (15, 30)

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


def _download_yahoo_batch_cached(tickers_tuple, interval, period):
    key = ("yahoo_batch", tuple(tickers_tuple), str(interval), str(period))
    cached = _ttl_cache_get(key, 180)
    if cached is not None:
        return cached
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
    _ttl_cache_set(key, data)
    return data.copy(deep=True)


def _yahoo_is_healthy():
    """Probe Yahoo directly; deliberately bypass the normal 180-second batch cache."""
    if yf is None:
        return False
    try:
        with _YF_LOCK:
            data = yf.download(
                "RELIANCE.NS", period="5d", interval="5m",
                auto_adjust=False, actions=False, progress=False,
                threads=False, timeout=10,
            )
        return isinstance(data, pd.DataFrame) and not data.empty
    except Exception:
        return False

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


def _liquidity_levels_and_sweeps(df, lookback=30, timeframe_minutes=15):
    """Detect liquidity levels and confirmed sweeps with their candle-close times in IST."""
    empty = {
        "Buy-side Liquidity": None, "Sell-side Liquidity": None,
        "Buy-side Liquidity Time (IST)": None, "Sell-side Liquidity Time (IST)": None,
        "Equal High Liquidity": "NO", "Equal Low Liquidity": "NO",
        "Upside Sweep": "NO", "Downside Sweep": "NO",
        "Upside Sweep Time (IST)": None, "Downside Sweep Time (IST)": None,
        "Liquidity Sweep Detail": "NO DATA",
        "Candle Volume": None, "Average Volume 20": None, "RVOL": None,
        "Volume Signal": "NO DATA", "Candle Direction": "WAIT",
    }
    required = {"Time", "Open", "High", "Low", "Close", "Volume"}
    if (df is None or not isinstance(df, pd.DataFrame) or not required.issubset(df.columns)
            or len(df) < max(lookback + 1, 25)):
        return empty
    d = df.tail(lookback + 1).reset_index(drop=True)
    prior, last = d.iloc[:-1], d.iloc[-1]
    highs = prior["High"].astype(float).to_numpy()
    lows = prior["Low"].astype(float).to_numpy()
    close_prev = float(prior["Close"].iloc[-1])
    close_now = _safe_num(last["Close"])
    swing_highs, swing_lows = [], []
    for i in range(1, len(prior)-1):
        if highs[i] >= highs[i-1] and highs[i] > highs[i+1]:
            swing_highs.append((i, highs[i]))
        if lows[i] <= lows[i-1] and lows[i] < lows[i+1]:
            swing_lows.append((i, lows[i]))
    if swing_highs:
        buy_idx, buy_liq = max(swing_highs, key=lambda item: item[1])
    else:
        buy_idx = int(np.argmax(highs))
        buy_liq = float(highs[buy_idx])
    if swing_lows:
        sell_idx, sell_liq = min(swing_lows, key=lambda item: item[1])
    else:
        sell_idx = int(np.argmin(lows))
        sell_liq = float(lows[sell_idx])
    eq_tol = max(abs(close_prev) * 0.0015, 1e-9)
    equal_high = any(abs(a[1]-b[1]) <= eq_tol for i, a in enumerate(swing_highs) for b in swing_highs[i+1:])
    equal_low = any(abs(a[1]-b[1]) <= eq_tol for i, a in enumerate(swing_lows) for b in swing_lows[i+1:])
    high, low = _safe_num(last["High"]), _safe_num(last["Low"])
    opn = _safe_num(last["Open"])
    span = max(high-low, 1e-9)
    upper_wick, lower_wick = high-max(opn, close_now), min(opn, close_now)-low
    vol_avg = _safe_num(prior["Volume"].tail(20).mean(), 0.0)
    rvol = _safe_num(last["Volume"] / vol_avg, 0.0) if vol_avg > 0 else 0.0
    candle_volume = _safe_num(last["Volume"], 0.0)
    candle_direction = "BUY" if close_now > opn else "SELL" if close_now < opn else "NEUTRAL"
    volume_signal = (
        "HIGH VOLUME BUY" if rvol >= 2.0 and candle_direction == "BUY" else
        "HIGH VOLUME SELL" if rvol >= 2.0 and candle_direction == "SELL" else
        "BELOW 2X THRESHOLD"
    )
    upside = bool(high > buy_liq and close_now < buy_liq and upper_wick/span >= .30
                  and close_now < opn and rvol >= 1.05)
    downside = bool(low < sell_liq and close_now > sell_liq and lower_wick/span >= .30
                    and close_now > opn and rvol >= 1.05)

    def _close_time(value):
        try:
            ts = pd.Timestamp(value)
            ts = ts.tz_localize(IST) if ts.tzinfo is None else ts.tz_convert(IST)
            if timeframe_minutes == 60 and ts.hour == 15 and ts.minute == 15:
                ts = ts.normalize() + SESSION_CLOSE
            else:
                ts += pd.Timedelta(minutes=int(timeframe_minutes))
            return ts.strftime("%Y-%m-%d %H:%M:%S IST")
        except Exception:
            return None

    buy_time = _close_time(prior["Time"].iloc[buy_idx])
    sell_time = _close_time(prior["Time"].iloc[sell_idx])
    trigger_time = _close_time(last["Time"])
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
        "Buy-side Liquidity Time (IST)": buy_time, "Sell-side Liquidity Time (IST)": sell_time,
        "Equal High Liquidity": "YES" if equal_high else "NO",
        "Equal Low Liquidity": "YES" if equal_low else "NO",
        "Upside Sweep": "YES" if upside else "NO", "Downside Sweep": "YES" if downside else "NO",
        "Upside Sweep Time (IST)": trigger_time if upside else None,
        "Downside Sweep Time (IST)": trigger_time if downside else None,
        "Liquidity Sweep Detail": detail,
        "Candle Volume": round(candle_volume, 2),
        "Average Volume 20": round(vol_avg, 2) if vol_avg > 0 else None,
        "RVOL": round(rvol, 2),
        "Volume Signal": volume_signal,
        "Candle Direction": candle_direction,
    }


def _breakout_breakdown_signal(df, lookback=20):
    """Require a completed candle close beyond the previous range with participation."""
    empty = {"Breakout Signal": "WAIT", "Breakout Level": None,
             "Breakdown Level": None, "Breakout Reason": "Insufficient completed candles"}
    required = {"Open", "High", "Low", "Close", "Volume"}
    if (df is None or not isinstance(df, pd.DataFrame)
            or not required.issubset(df.columns)
            or not isinstance(lookback, int) or lookback < 2
            or len(df) < lookback + 1):
        return empty
    # Exactly `lookback` completed candles form the reference range and the
    # latest completed candle is the trigger: lookback + 1 bars in total.
    d = df.tail(lookback + 1).reset_index(drop=True)
    base, last = d.iloc[-(lookback+1):-1], d.iloc[-1]
    resistance, support = _safe_num(base["High"].max(), np.nan), _safe_num(base["Low"].min(), np.nan)
    o, h, l, c = (_safe_num(last[k]) for k in ("Open", "High", "Low", "Close"))
    span = max(h-l, 1e-9)
    body = abs(c-o)/span
    close_loc = (c-l)/span
    avg_vol = _safe_num(base["Volume"].tail(20).mean(), 0.0)
    rvol = _safe_num(last["Volume"]/avg_vol, 0.0) if avg_vol > 0 else 0.0
    if c > resistance and c > o and body >= .55 and close_loc >= .75 and rvol >= 1.5:
        sig, reason = "BREAKOUT", f"Completed close above swing resistance {resistance:.2f}; body {body:.0%}; RVOL {rvol:.2f}x"
    elif c < support and c < o and body >= .55 and close_loc <= .25 and rvol >= 1.5:
        sig, reason = "BREAKDOWN", f"Completed close below swing support {support:.2f}; body {body:.0%}; RVOL {rvol:.2f}x"
    else:
        sig, reason = "WAIT", f"No confirmed range break; body {body:.0%}; RVOL {rvol:.2f}x"
    return {"Breakout Signal": sig, "Breakout Level": round(resistance, 2),
            "Breakdown Level": round(support, 2), "Breakout Reason": reason}


def _multi_tf_breakout(frames):
    """A single timeframe trigger is provisional; require 2/3 aligned TFs for confirmation."""
    checks = {tf: _breakout_breakdown_signal(frames.get(tf, pd.DataFrame())) for tf in TIMEFRAMES}
    ups = sum(v["Breakout Signal"] == "BREAKOUT" for v in checks.values())
    dns = sum(v["Breakout Signal"] == "BREAKDOWN" for v in checks.values())
    if ups >= 2 and dns == 0:
        signal = "BREAKOUT CONFIRMED"
    elif dns >= 2 and ups == 0:
        signal = "BREAKDOWN CONFIRMED"
    elif ups or dns:
        signal = "PROVISIONAL — WAIT"
    else:
        signal = "WAIT"
    return {"Multi-TF Breakout": signal,
            "5M Breakout": checks["5M"]["Breakout Signal"],
            "15M Breakout": checks["15M"]["Breakout Signal"],
            "1H Breakout": checks["1H"]["Breakout Signal"]}


def _breakout_retest_confirmation(df, direction, lookback=20, scan=6, tol=0.003):
    """Confirm a retest only when price tests the level zone and closes on its valid side."""
    required = {"Time", "Open", "High", "Low", "Close", "Volume"}
    if (df is None or not isinstance(df, pd.DataFrame)
            or not required.issubset(df.columns)
            or not isinstance(lookback, int) or lookback < 2
            or not isinstance(scan, int) or scan < 1
            or not np.isfinite(tol) or tol < 0):
        return "WAIT — NO RETEST DATA"
    if direction not in ("UP", "DOWN"):
        return "WAIT — INVALID DIRECTION"
    try:
        d = df.sort_values("Time").drop_duplicates("Time", keep="last").reset_index(drop=True)
        n = len(d)
        if n < lookback + 2:
            return "WAIT — NO RETEST DATA"
        for col in ("Open", "High", "Low", "Close", "Volume"):
            d[col] = pd.to_numeric(d[col], errors="coerce")
        d = d.dropna(subset=["Open", "High", "Low", "Close", "Volume"]).reset_index(drop=True)
        n = len(d)
        if n < lookback + 2:
            return "WAIT — NO RETEST DATA"

        first_k = max(lookback, n - scan - 2)
        for k in range(n - 2, first_k - 1, -1):
            prior = d.iloc[k-lookback:k]
            candle = d.iloc[k]
            after = d.iloc[k+1:]
            if after.empty:
                continue
            up_level = float(prior["High"].max())
            down_level = float(prior["Low"].min())
            span = max(float(candle["High"])-float(candle["Low"]), 1e-9)
            body_ratio = abs(float(candle["Close"])-float(candle["Open"])) / span
            prior_vol = float(prior["Volume"].mean())
            breakout_rvol = float(candle["Volume"])/prior_vol if prior_vol > 0 else 0.0
            strong_break = body_ratio >= 0.55 and breakout_rvol >= 1.5

            if direction == "UP" and float(candle["Close"]) > up_level and strong_break:
                # The retest low must stay within the tolerance band around
                # resistance; a deep loss of the level is not a successful retest.
                touched = (
                    (after["Low"] <= up_level * (1 + tol))
                    & (after["Low"] >= up_level * (1 - tol))
                    & (after["Close"] > up_level)
                ).any()
                held = float(d["Close"].iloc[-1]) > up_level
                return "RETEST HELD" if touched and held else "WAIT — RETEST NOT CONFIRMED"

            if direction == "DOWN" and float(candle["Close"]) < down_level and strong_break:
                # The retest high must stay within the tolerance band around
                # support; a strong reclaim above support invalidates rejection.
                touched = (
                    (after["High"] >= down_level * (1 - tol))
                    & (after["High"] <= down_level * (1 + tol))
                    & (after["Close"] < down_level)
                ).any()
                held = float(d["Close"].iloc[-1]) < down_level
                return "RETEST REJECTED" if touched and held else "WAIT — RETEST NOT CONFIRMED"
        return "WAIT — NO RECENT BREAKOUT"
    except Exception:
        return "WAIT — RETEST DATA ERROR"


def _last_session_close(now=None, minutes=None):
    """Latest expected weekday close; 60M data excludes the 15:15 partial bar."""
    now = pd.Timestamp(now if now is not None else pd.Timestamp.now(tz=IST))
    now = now.tz_localize(IST) if now.tzinfo is None else now.tz_convert(IST)
    day = now.normalize()
    if now.weekday() >= 5:
        day -= pd.Timedelta(days=now.weekday() - 4)
    elif now.time() < pd.Timestamp("09:15").time():
        day -= pd.Timedelta(days=1)
    while day.weekday() >= 5 or day.strftime("%Y-%m-%d") in NSE_HOLIDAYS:
        day -= pd.Timedelta(days=1)
    expected_close = pd.Timedelta(hours=15, minutes=15) if minutes == 60 else SESSION_CLOSE
    return day + expected_close


def _cached_candles_are_fresh(frame, minutes, now=None):
    """Use cached bars until the next interval could have completed; tolerate Yahoo delay."""
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
        # During the opening interval, no new completed candle exists yet.
        open_time = now.normalize() + pd.Timedelta(hours=9, minutes=15)
        if open_time <= now < open_time + pd.Timedelta(minutes=minutes):
            previous_close = _last_session_close(now.normalize() + pd.Timedelta(hours=9), minutes=minutes)
            return close_time >= previous_close - pd.Timedelta(minutes=1)
        if minutes == 60 and pd.Timestamp("09:15").time() <= now.time() < pd.Timestamp("10:15").time():
            previous_close = _last_session_close(now.normalize() + pd.Timedelta(hours=9), minutes=60)
            return close_time >= previous_close - pd.Timedelta(minutes=1)
        if not _is_market_hours(now.to_pydatetime()):
            return close_time >= (_last_session_close(now, minutes=minutes) - pd.Timedelta(minutes=1))
        age = (now - close_time).total_seconds() / 60.0
        return -1.0 <= age < float(minutes) + 2.0
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
    if str(timeframe).upper() == "1H" and "Time" in d.columns:
        try:
            times = pd.to_datetime(d["Time"], errors="coerce")
            latest = times.iloc[-1]
            same_slot = (
                (times.dt.hour == latest.hour) &
                (times.dt.minute == latest.minute) &
                (times.dt.date < latest.date())
            )
            slot_volumes = pd.to_numeric(v[same_slot], errors="coerce").dropna().tail(10)
            # Require several historical same-slot observations; otherwise use the
            # ordinary rolling baseline rather than a noisy one-day comparison.
            if len(slot_volumes) >= 3:
                avg_vol = _safe_num(slot_volumes.mean(), avg_vol)
        except Exception:
            pass
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
    session_bar_count = len(d)
    if str(timeframe).upper() == "1H" and "Time" in d.columns:
        try:
            latest_date = pd.to_datetime(d["Time"].iloc[-1]).date()
            session_bar_count = int((pd.to_datetime(d["Time"]).dt.date == latest_date).sum())
        except Exception:
            session_bar_count = 0
    if str(timeframe).upper() == "1H" and session_bar_count < 2:
        result["VWAP"] = _agent_result("WAIT", 0, "1H session VWAP warm-up; need at least 2 completed bars")
    elif pd.notna(vwap) and last > vwap * 1.001:
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
        later = d.iloc[-(len(ob_window)-pos):]
        if later.empty:
            continue
        if candidate["Close"] < candidate["Open"] and later["Close"].max() > candidate["High"]:
            bullish_ob = (float(candidate["Low"]), float(candidate["High"]))
            break
    for pos in range(len(ob_window) - 1, -1, -1):
        candidate = ob_window.iloc[pos]
        later = d.iloc[-(len(ob_window)-pos):]
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
# Verb forms are listed explicitly (jumps/jumped/surges...) because the patterns are
# wrapped in \b...\b; a bare "jump" would not match "jumps".
_POSITIVE = [
    r"profits? (?:rises?|rose|jumps?|jumped|surges?|surged|increases?|increased|up|grow(?:s|ing)?)",
    r"revenue (?:rises?|rose|jumps?|jumped|surges?|surged|increases?|increased|up|grow(?:s|ing)?)",
    r"growth (?:accelerates?|improves?|strengthens?|surges?)",
    r"(?:analyst|brokerage) (?:upgrades?|raises? rating)",
    r"upgraded to (?:buy|overweight|outperform)",
    r"order wins?", r"regulatory approval", r"record (?:profit|revenue|orders?|highs?)",
    r"share buyback", r"dividend (?:increase|hike|announced)", r"strong results",
    r"beats? (?:market|street|analyst|estimates?|expectations?)",
    r"debt reduced", r"margin expansion", r"capacity expansion", r"positive outlook",
]
_NEGATIVE = [
    r"net loss(?:es)?",
    r"profits? (?:falls?|fell|declines?|declined|drops?|dropped|down|slumps?)",
    r"revenue (?:falls?|fell|declines?|declined|drops?|dropped|down|slumps?)",
    r"downgrad(?:e|ed|es)",
    r"fraud", r"default(?:s|ed)?", r"(?:regulatory|investigation) probe",
    r"ban(?:s|ned)?", r"penalt(?:y|ies)", r"resignation", r"weak results",
    r"misses? (?:market|street|analyst|estimates?|expectations?)",
    r"debt (?:default|restructuring|crisis)", r"guidance cut", r"margin contraction",
    r"promoter pledge", r"insolvency", r"accounting irregularit(?:y|ies)",
]
_POS_RE = [re.compile(r"\b" + p + r"\b") for p in _POSITIVE]
_NEG_RE = [re.compile(r"\b" + p + r"\b") for p in _NEGATIVE]


def _fetch_yahoo_news_cached(yahoo_symbol):
    key = ("yahoo_news", str(yahoo_symbol).upper())
    cached = _ttl_cache_get(key, 300)
    if cached is not None:
        return cached
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
        _ttl_cache_set(key, cleaned)
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
        # Normalize Yahoo headline punctuation/HTML before phrase matching. Keep
        # phrase boundaries so e.g. "upgrade" does not match "downgrade".
        headline = str(item.get("title", "")).lower()
        headline = re.sub(r"<[^>]+>", " ", headline)
        headline = re.sub(r"&(?:amp|nbsp|quot);", " ", headline)
        headline = re.sub(r"[^a-z0-9%+.-]+", " ", headline)
        headline = re.sub(r"\s+", " ", headline).strip()
        positive_hits = sum(bool(p.search(headline)) for p in _POS_RE)
        negative_hits = sum(bool(p.search(headline)) for p in _NEG_RE)
        score = positive_hits - negative_hits
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
    """5M same-session compression + volume watch; requires 13 completed candles today."""
    empty = {
        "PRE-BIG Status": "NO DATA", "PRE-BIG Direction": "WAIT",
        "PRE-BIG Score": 0, "PRE-BIG Candles": 0,
        "PRE-BIG Range High": None, "PRE-BIG Range Low": None,
        "PRE-BIG Range Width %": None, "PRE-BIG Volume Build %": None,
        "PRE-BIG RVOL": None, "PRE-BIG Trigger": None,
        "PRE-BIG Reason": "Need at least 13 completed 5M candles today",
    }
    required = {"Time", "Open", "High", "Low", "Close", "Volume"}
    if df is None or not isinstance(df, pd.DataFrame) or not required.issubset(df.columns):
        return empty
    full = df.sort_values("Time").drop_duplicates("Time", keep="last").reset_index(drop=True)
    if full.empty:
        return empty
    latest_day = pd.Timestamp(full["Time"].iloc[-1]).date()
    same_session = full[pd.to_datetime(full["Time"]).dt.date == latest_day].reset_index(drop=True)
    candle_count = len(same_session)
    if candle_count < 13:
        warm = empty.copy()
        warm["PRE-BIG Status"] = "WAIT — SESSION WARM-UP"
        warm["PRE-BIG Reason"] = f"Need 13 same-session 5M candles; have {candle_count}"
        warm["PRE-BIG Candles"] = candle_count
        return warm

    session_vwap = _vwap_last_session(same_session)
    base = same_session.iloc[-13:-1].copy()  # 12 same-day base candles
    last = same_session.iloc[-1]
    hi = _safe_num(base["High"].max(), np.nan)
    lo = _safe_num(base["Low"].min(), np.nan)
    width = hi - lo
    close = _safe_num(last["Close"], np.nan)
    if not np.isfinite(hi) or not np.isfinite(lo) or width <= 0 or close <= 0:
        return empty
    width_pct = width / close * 100.0

    # Compare only with today's earlier candles. Before 25 same-session bars,
    # use a volatility-aware absolute compression cap instead of yesterday's range.
    if candle_count >= 25:
        prior = same_session.iloc[-25:-13]
        prior_width = _safe_num(prior["High"].max() - prior["Low"].min(), 0.0)
        compression = prior_width > 0 and width <= prior_width * 0.70
        compression_reason = "same-session relative contraction"
    else:
        h = same_session["High"].astype(float)
        l = same_session["Low"].astype(float)
        c = same_session["Close"].astype(float)
        prev_c = c.shift(1)
        tr = pd.concat([(h-l), (h-prev_c).abs(), (l-prev_c).abs()], axis=1).max(axis=1)
        atr5 = _safe_num(tr.tail(14).mean(), 0.0)
        compression = width_pct <= 0.8 or (atr5 > 0 and width <= 2.5 * atr5)
        compression_reason = f"absolute early-session cap (width {width_pct:.2f}%, ATR5 {atr5:.3f})"

    vol_early = _safe_num(base["Volume"].iloc[:9].mean(), 0.0)
    vol_recent = _safe_num(base["Volume"].iloc[-3:].mean(), 0.0)
    volume_build = vol_recent / vol_early if vol_early > 0 else 0.0
    avg20 = _safe_num(same_session.iloc[:-1]["Volume"].tail(20).mean(), 0.0)
    last_vol = _safe_num(last["Volume"], 0.0)
    rvol = last_vol / avg20 if avg20 > 0 else 0.0
    base_close = _safe_num(base["Close"].iloc[-1], close)
    vwap = _safe_num(session_vwap, np.nan)
    recent_lows = base["Low"].astype(float).tail(4).to_numpy()
    recent_highs = base["High"].astype(float).tail(4).to_numpy()
    higher_lows = len(recent_lows) >= 3 and recent_lows[-1] >= recent_lows[0]
    lower_highs = len(recent_highs) >= 3 and recent_highs[-1] <= recent_highs[0]
    upper_zone = base_close >= lo + width * 0.65
    lower_zone = base_close <= lo + width * 0.35
    vwap_bull = np.isfinite(vwap) and base_close >= vwap
    vwap_bear = np.isfinite(vwap) and base_close <= vwap
    o, h, l, c = (_safe_num(last[k]) for k in ("Open", "High", "Low", "Close"))
    span = max(h-l, 1e-9)
    body = abs(c-o) / span
    close_loc = (c-l) / span
    breakout_up = c > hi and c > o and body >= 0.55 and close_loc >= 0.75 and rvol >= 1.5
    breakout_dn = c < lo and c < o and body >= 0.55 and close_loc <= 0.25 and rvol >= 1.5
    score = 30 if compression else 0
    score += 25 if volume_build >= 1.30 else 15 if volume_build >= 1.15 else 0
    score += 20 if rvol >= 1.50 else 10 if rvol >= 1.20 else 0
    bull_points = int(upper_zone) + int(higher_lows) + int(vwap_bull)
    bear_points = int(lower_zone) + int(lower_highs) + int(vwap_bear)
    score += 25 if max(bull_points, bear_points) >= 2 else 10 if max(bull_points, bear_points) == 1 else 0

    # Do not call an already-escaped price a PRE-BIG watch.
    if c > hi or c < lo:
        if breakout_up:
            status, direction, reason, trigger = "BULLISH BREAKOUT CONFIRMED", "UP", "Completed 5M close above compressed range with strong body and RVOL", round(hi, 2)
            score = max(score, 85)
        elif breakout_dn:
            status, direction, reason, trigger = "BEARISH BREAKDOWN CONFIRMED", "DOWN", "Completed 5M close below compressed range with strong body and RVOL", round(lo, 2)
            score = max(score, 85)
        else:
            status, direction, reason, trigger = "MOVE ALREADY STARTED — NO PRE-BIG WATCH", "WAIT", "Latest completed close is outside the base range but lacks breakout confirmation", None
    elif compression and volume_build >= 1.30 and rvol >= 1.20 and bull_points >= 2:
        status, direction, reason, trigger = "PRE-BIG BULLISH WATCH", "UP", f"12-bar same-session compression + rising volume + bullish alignment; {compression_reason}", round(hi, 2)
    elif compression and volume_build >= 1.30 and rvol >= 1.20 and bear_points >= 2:
        status, direction, reason, trigger = "PRE-BIG BEARISH WATCH", "DOWN", f"12-bar same-session compression + rising volume + bearish alignment; {compression_reason}", round(lo, 2)
    else:
        status, direction, reason, trigger = "WAIT", "WAIT", f"Compression, volume and directional conditions are not aligned; {compression_reason}", None
    return {
        "PRE-BIG Status": status, "PRE-BIG Direction": direction,
        "PRE-BIG Score": int(min(100, score)), "PRE-BIG Candles": candle_count,
        "PRE-BIG Range High": round(hi, 2), "PRE-BIG Range Low": round(lo, 2),
        "PRE-BIG Range Width %": round(width_pct, 3),
        "PRE-BIG Volume Build %": round((volume_build-1)*100, 1),
        "PRE-BIG RVOL": round(rvol, 2), "PRE-BIG Trigger": trigger,
        "PRE-BIG Reason": reason,
    }

def _advanced_big_movement_analysis(frame_5m, frame_15m=None, frame_1h=None):
    """Rule-based big-move context; screening score is not probability."""
    empty = {
        "Advanced Big Move": "WAIT", "Big Move Direction": "WAIT",
        "Big Move Score": 0, "False Breakout Risk": "UNKNOWN",
        "Breakout Retest Status": "NOT TESTED", "Momentum Continuation": "UNKNOWN",
        "5M Retest Check": "NO DATA", "15M Trend Check": "NO DATA",
        "Big Move Range High": None, "Big Move Range Low": None,
        "Big Move RVOL": None, "Big Move Reason": "Insufficient completed 5M candles",
    }
    required = {"Time", "Open", "High", "Low", "Close", "Volume"}
    if frame_5m is None or not isinstance(frame_5m, pd.DataFrame) or not required.issubset(frame_5m.columns):
        return empty
    try:
        d = frame_5m.sort_values("Time").drop_duplicates("Time", keep="last").reset_index(drop=True)
        if d.empty:
            return empty
        today = pd.Timestamp(d["Time"].iloc[-1]).date()
        session = d[pd.to_datetime(d["Time"]).dt.date == today].reset_index(drop=True)
        if len(session) < 13:
            out = empty.copy()
            out["Advanced Big Move"] = "WAIT — SESSION WARM-UP"
            out["Big Move Reason"] = f"Need 13 completed 5M candles today; have {len(session)}"
            return out
        base = session.iloc[-13:-1]
        last = session.iloc[-1]
        hi, lo = float(base["High"].astype(float).max()), float(base["Low"].astype(float).min())
        close, opn = float(last["Close"]), float(last["Open"])
        high, low = float(last["High"]), float(last["Low"])
        if close <= 0 or hi <= lo:
            return empty
        vol_early = float(base["Volume"].astype(float).iloc[:9].mean())
        vol_recent = float(base["Volume"].astype(float).iloc[-3:].mean())
        rvol_base = float(last["Volume"]) / max(float(session["Volume"].iloc[:-1].tail(20).mean()), 1e-9)
        volume_build = vol_recent / max(vol_early, 1e-9)
        width_pct = (hi-lo) / close * 100.0
        if len(session) >= 25:
            prior = session.iloc[-25:-13]
            prior_width = float(prior["High"].max()-prior["Low"].min()) if len(prior) else 0.0
            compressed = prior_width > 0 and (hi-lo) <= prior_width * 0.75
            compression_reason = "same-session relative contraction"
        else:
            h_s, l_s, c_s = session["High"].astype(float), session["Low"].astype(float), session["Close"].astype(float)
            prev_c = c_s.shift(1)
            tr = pd.concat([(h_s-l_s), (h_s-prev_c).abs(), (l_s-prev_c).abs()], axis=1).max(axis=1)
            atr5 = _safe_num(tr.tail(14).mean(), 0.0)
            compressed = width_pct <= 0.8 or (atr5 > 0 and (hi-lo) <= 2.5*atr5)
            compression_reason = f"absolute early-session cap (width {width_pct:.2f}%, ATR5 {atr5:.3f})"
        span = max(high-low, 1e-9)
        body_ratio = abs(close-opn)/span
        close_location = (close-low)/span
        broke_up, broke_down = close > hi, close < lo
        prev = session.iloc[-2] if len(session) >= 2 else None
        pre = session.iloc[-14:-2] if len(session) >= 14 else None
        if prev is not None and pre is not None:
            pre_hi, pre_lo = float(pre["High"].max()), float(pre["Low"].min())
            failed_up = float(prev["High"]) > pre_hi and close <= pre_hi
            failed_down = float(prev["Low"]) < pre_lo and close >= pre_lo
        else:
            failed_up = failed_down = False
        weak_up = broke_up and (body_ratio < 0.45 or close_location < 0.65 or rvol_base < 1.25)
        weak_down = broke_down and (body_ratio < 0.45 or close_location > 0.35 or rvol_base < 1.25)
        false_risk = "HIGH" if (failed_up or failed_down or weak_up or weak_down) else (
            "LOW" if broke_up or broke_down else "MEDIUM" if compressed and rvol_base >= 1.2 else "UNKNOWN")
        ema9 = float(session["Close"].astype(float).ewm(span=9, adjust=False).mean().iloc[-1])
        ema20 = float(session["Close"].astype(float).ewm(span=20, adjust=False).mean().iloc[-1])
        up_align = down_align = 0
        for tf_frame in (frame_15m, frame_1h):
            if tf_frame is None or not isinstance(tf_frame, pd.DataFrame) or len(tf_frame) < 20 or "Close" not in tf_frame:
                continue
            c_tf = tf_frame["Close"].astype(float)
            e9 = float(c_tf.ewm(span=9, adjust=False).mean().iloc[-1])
            e20 = float(c_tf.ewm(span=20, adjust=False).mean().iloc[-1])
            up_align += int(float(c_tf.iloc[-1]) > e9 > e20)
            down_align += int(float(c_tf.iloc[-1]) < e9 < e20)
        up_setup = int(compressed) + int(volume_build >= 1.20) + int(rvol_base >= 1.20) + int(ema9 > ema20) + up_align
        down_setup = int(compressed) + int(volume_build >= 1.20) + int(rvol_base >= 1.20) + int(ema9 < ema20) + down_align
        # 5M is the entry timeframe: a breakout is only DETECTED first.
        # A BUY/SELL confirmation needs a later 5M retest AND 15M EMA trend alignment.
        retest_up = _breakout_retest_confirmation(session, "UP", lookback=12, scan=6, tol=0.003)
        retest_down = _breakout_retest_confirmation(session, "DOWN", lookback=12, scan=6, tol=0.003)
        f15 = frame_15m if isinstance(frame_15m, pd.DataFrame) else pd.DataFrame()
        up_15m_ok = down_15m_ok = False
        if not f15.empty and "Close" in f15.columns:
            c15 = pd.to_numeric(f15["Close"], errors="coerce").dropna()
            if len(c15) >= 20:
                e9_15 = float(c15.ewm(span=9, adjust=False).mean().iloc[-1])
                e20_15 = float(c15.ewm(span=20, adjust=False).mean().iloc[-1])
                last15 = float(c15.iloc[-1])
                up_15m_ok = last15 > e9_15 > e20_15
                down_15m_ok = last15 < e9_15 < e20_15

        retest = "NOT TESTED"
        if failed_up:
            status, direction, reason, retest = "FALSE BREAKOUT RISK", "WAIT", "Price moved above resistance but a later completed 5M candle closed back inside the range", "FAILED UPSIDE BREAK"
        elif failed_down:
            status, direction, reason, retest = "FALSE BREAKOUT RISK", "WAIT", "Price moved below support but a later completed 5M candle closed back inside the range", "FAILED DOWNSIDE BREAK"
        elif weak_up or weak_down:
            status, direction = "FALSE BREAKOUT RISK", "WAIT"
            reason, retest = "Price closed outside range but breakout candle/volume confirmation is weak", "WEAK BREAKOUT — NO ENTRY"
        elif broke_up:
            status, direction = "BREAKOUT DETECTED", "UP"
            reason, retest = "Completed 5M candle closed above the base range; wait for a 5M retest", "RETEST WAITING"
        elif broke_down:
            status, direction = "BREAKDOWN DETECTED", "DOWN"
            reason, retest = "Completed 5M candle closed below the base range; wait for a 5M retest", "RETEST WAITING"
        elif retest_up == "RETEST HELD":
            if up_15m_ok:
                status, direction = "BUY CONFIRMED", "UP"
                reason, retest = "5M breakout retest held and 15M EMA trend is bullish", "5M RETEST HELD + 15M UP ALIGNED"
            else:
                status, direction = "RETEST HELD — 15M WAIT", "WAIT"
                reason, retest = "5M retest held, but 15M trend alignment is not bullish yet", "5M RETEST HELD — WAIT 15M"
        elif retest_down == "RETEST REJECTED":
            if down_15m_ok:
                status, direction = "SELL CONFIRMED", "DOWN"
                reason, retest = "5M breakdown retest rejected and 15M EMA trend is bearish", "5M RETEST REJECTED + 15M DOWN ALIGNED"
            else:
                status, direction = "RETEST REJECTED — 15M WAIT", "WAIT"
                reason, retest = "5M breakdown retest rejected, but 15M trend alignment is not bearish yet", "5M RETEST REJECTED — WAIT 15M"
        elif retest_up == "WAIT — RETEST NOT CONFIRMED":
            status, direction, reason, retest = "RETEST WAITING", "UP", "A recent 5M upside breakout exists; waiting for a valid retest hold", "RETEST WAITING"
        elif retest_down == "WAIT — RETEST NOT CONFIRMED":
            status, direction, reason, retest = "RETEST WAITING", "DOWN", "A recent 5M downside breakout exists; waiting for a valid retest rejection", "RETEST WAITING"
        elif compressed and max(up_setup, down_setup) >= 4:
            direction = "UP" if up_setup > down_setup else "DOWN" if down_setup > up_setup else "WAIT"
            status = "PRE-BIG WATCH" if direction != "WAIT" else "WAIT"
            reason = "Compression + volume activity + EMA/multi-timeframe alignment; breakout not yet detected"
        else:
            status, direction, reason = "WAIT", "WAIT", "No strong compression/volume/trend confluence"

        score = min(100, int(max(up_setup, down_setup) / 6 * 100))
        if status in ("BUY CONFIRMED", "SELL CONFIRMED"):
            score = max(score, 85)
        elif status in ("BREAKOUT DETECTED", "BREAKDOWN DETECTED", "RETEST WAITING"):
            score = min(score, 65)
        if false_risk == "HIGH" and status not in ("BUY CONFIRMED", "SELL CONFIRMED"):
            score = min(score, 55)
        continuation = "BULLISH" if direction == "UP" and ema9 > ema20 and up_align >= 1 else (
            "BEARISH" if direction == "DOWN" and ema9 < ema20 and down_align >= 1 else "NOT CONFIRMED")
        return {
            "Advanced Big Move": status, "Big Move Direction": direction,
            "Big Move Score": score, "False Breakout Risk": false_risk,
            "Breakout Retest Status": retest, "Momentum Continuation": continuation,
            "5M Retest Check": retest_up if direction == "UP" else retest_down if direction == "DOWN" else "NOT APPLICABLE",
            "15M Trend Check": "UP ALIGNED" if up_15m_ok else "DOWN ALIGNED" if down_15m_ok else "NOT ALIGNED",
            "Big Move Range High": round(hi, 2), "Big Move Range Low": round(lo, 2),
            "Big Move RVOL": round(rvol_base, 2),
            "Big Move Reason": f"{reason}; volume build {volume_build:.2f}x; range {width_pct:.2f}%; {compression_reason}",
        }
    except Exception as exc:
        out = empty.copy()
        out["Big Move Reason"] = f"Advanced analysis error: {str(exc)[:160]}"
        return out

def _directional_pre_move_engine(df):
    """Early UP/DOWN watch from completed 5M candles; not a guaranteed signal."""
    empty = {
        "Directional Pre-Move": "NO DATA", "Pre-Move Direction": "WAIT",
        "UP Pre-Move Score": 0, "DOWN Pre-Move Score": 0,
        "Pre-Move Volume Build %": None, "Pre-Move Compression": "UNKNOWN",
        "Pre-Move 15M Alignment": "UNKNOWN",
        "Pre-Move Reason": "Need at least 13 completed 5M candles today",
    }
    required = {"Time", "Open", "High", "Low", "Close", "Volume"}
    if df is None or not isinstance(df, pd.DataFrame) or len(df) < 13 or not required.issubset(df.columns):
        return empty
    try:
        full = df.sort_values("Time").drop_duplicates("Time", keep="last").reset_index(drop=True)
        latest_day = pd.Timestamp(full["Time"].iloc[-1]).date()
        same_session = full[pd.to_datetime(full["Time"]).dt.date == latest_day]
        # Base window (12) + trigger candle must be same-session; avoids gap-candle false setups.
        if len(same_session) < 13:
            warm = empty.copy()
            warm["Directional Pre-Move"] = "WAIT — SESSION WARM-UP"
            warm["Pre-Move Reason"] = f"Need 13 same-session 5M candles; have {len(same_session)}"
            return warm
        session_vwap = _vwap_last_session(same_session)
        recent, last = same_session.iloc[-13:-1], same_session.iloc[-1]
        prior = same_session.iloc[-25:-13] if len(same_session) >= 25 else same_session.iloc[0:0]
        old_width = _safe_num(prior["High"].max() - prior["Low"].min(), 0.0) if len(prior) else 0.0
        recent_high = _safe_num(recent["High"].max(), 0.0)
        recent_low = _safe_num(recent["Low"].min(), 0.0)
        recent_width = recent_high - recent_low
        close = _safe_num(last["Close"], 0.0)
        if recent_width <= 0 or close <= 0:
            return empty

        if len(prior) == 12 and old_width > 0:
            compression = recent_width <= old_width * 0.80
        else:
            hs, ls, cs = same_session["High"].astype(float), same_session["Low"].astype(float), same_session["Close"].astype(float)
            prev_cs = cs.shift(1)
            tr = pd.concat([(hs-ls), (hs-prev_cs).abs(), (ls-prev_cs).abs()], axis=1).max(axis=1)
            atr5 = _safe_num(tr.tail(14).mean(), 0.0)
            compression = (recent_width / close * 100.0) <= 0.8 or (atr5 > 0 and recent_width <= 2.5 * atr5)
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
    ordered = df.sort_values("Time").drop_duplicates("Time", keep="last").reset_index(drop=True)
    latest_day = pd.Timestamp(ordered["Time"].iloc[-1]).date()
    same_session = ordered[pd.to_datetime(ordered["Time"]).dt.date == latest_day]
    if len(same_session) < 21:
        warm = empty.copy()
        warm["20M Status"] = "WAIT — SESSION WARM-UP"
        warm["20M Reason"] = f"Session warm-up: {len(same_session)}/21 same-session 1M candles"
        return warm
    d = same_session.tail(21).reset_index(drop=True)
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
    rvol = _safe_num(last["Volume"]) / max(_safe_num(base["Volume"].mean()), 1.0)
    buildup = recent_avg / max(prev_avg, 1.0)
    candle_span = max(_safe_num(last["High"] - last["Low"]), 1e-9)
    close_location = max(0.0, min(1.0, (close - _safe_num(last["Low"])) / candle_span))
    pressure = (close_location - 0.5) * 200.0
    last_open = _safe_num(last["Open"])
    body_ratio = abs(close - last_open) / candle_span
    breakout_up = (
        close > high and close > last_open and body_ratio >= 0.55
        and close_location >= 0.75 and rvol >= 1.50
    )
    breakout_down = (
        close < low and close < last_open and body_ratio >= 0.55
        and close_location <= 0.25 and rvol >= 1.50
    )
    # Compare only within this session; never let yesterday's range leak into
    # today's 20M compression test.
    prior_compression = False
    if len(same_session) >= 41:
        prior = same_session.iloc[-41:-21]
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
        reason = "Completed 1M close broke above 20-candle high with body >=55%, close near high and RVOL >=1.5"
        score = max(score, 80)
    elif breakout_down:
        status, direction = "CONFIRMED BREAKOUT", "DOWN"
        reason = "Completed 1M close broke below 20-candle low with body >=55%, close near low and RVOL >=1.5"
        score = max(score, 80)
    elif close > high or close < low:
        status = "BREAKOUT WATCH — VOLUME/BODY NOT CONFIRMED"
        direction = "UP" if close > high else "DOWN"
        reason = f"Range crossed, but confirmation failed (body {body_ratio:.2f}, RVOL {rvol:.2f}); wait for stronger completed candle"
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



def _early_signal_decision(row):
    """Additive early-decision layer; never changes the legacy Final/Action Signal."""
    empty = {
        "Early Signal": "WAIT", "Early Direction": "WAIT",
        "Early Evidence Score": 0, "Early Confirmation": "NOT CONFIRMED",
        "Early Signal Reason": "Insufficient completed-candle evidence",
        "Early Signal Time (IST)": None, "1M Candle Close (IST)": None,
    }
    try:
        one_dir = str(row.get("20M Direction", "WAIT") or "WAIT").upper()
        one_status = str(row.get("20M Status", "NO DATA") or "NO DATA").upper()
        one_score = max(0.0, min(100.0, _safe_num(row.get("20M Pre-Move Score"), 0.0)))
        five_dir = str(row.get("Pre-Move Direction", "WAIT") or "WAIT").upper()
        five_status = str(row.get("Directional Pre-Move", "NO DATA") or "NO DATA").upper()
        five_score = _safe_num(row.get("UP Pre-Move Score" if five_dir == "UP" else "DOWN Pre-Move Score"), 0.0)
        prebig_dir = str(row.get("PRE-BIG Direction", "WAIT") or "WAIT").upper()
        prebig_status = str(row.get("PRE-BIG Status", "NO DATA") or "NO DATA").upper()
        if five_dir not in ("UP", "DOWN") and prebig_dir in ("UP", "DOWN") and "WATCH" in prebig_status:
            five_dir = prebig_dir
            five_score = _safe_num(row.get("PRE-BIG Score"), 0.0)
        d15 = str(row.get("15M", "WAIT") or "WAIT").upper()
        d1h = str(row.get("1H", "WAIT") or "WAIT").upper()
        direction_map = {"BUY": "UP", "SELL": "DOWN"}
        tf15 = direction_map.get(d15, "WAIT")
        tf1h = direction_map.get(d1h, "WAIT")
        one_is_candidate = one_dir in ("UP", "DOWN") and any(x in one_status for x in (
            "WATCH", "BREAKOUT WATCH", "CONFIRMED BREAKOUT"
        ))
        five_is_candidate = five_dir in ("UP", "DOWN") and (
            "PRE-MOVE WATCH" in five_status or "PRE-BIG" in prebig_status and "WATCH" in prebig_status
        )
        directions = [d for d, ok in ((one_dir, one_is_candidate), (five_dir, five_is_candidate)) if ok]
        if not directions:
            out = empty.copy()
            if "WARM-UP" in one_status or "WARM-UP" in five_status:
                out["Early Signal"] = "WAIT — SESSION WARM-UP"
                out["Early Signal Reason"] = f"1M: {one_status}; 5M: {five_status}"
            elif "NO DATA" in (one_status, five_status):
                out["Early Signal"] = "WAIT — EARLY DATA MISSING"
                out["Early Signal Reason"] = f"1M: {one_status}; 5M: {five_status}"
            else:
                out["Early Signal Reason"] = f"No valid early setup. 1M: {one_status}; 5M: {five_status}"
            return out
        if len(set(directions)) > 1:
            return {**empty, "Early Signal": "WAIT — 1M/5M CONFLICT",
                    "Early Signal Reason": f"1M direction {one_dir} conflicts with 5M direction {five_dir}"}
        direction = directions[0]
        if tf15 != direction:
            return {**empty, "Early Signal": "WAIT — 15M NOT ALIGNED", "Early Direction": direction,
                    "Early Signal Reason": f"Early direction {direction}; 15M trend is {d15}. Wait for 15M alignment."}
        if tf1h in ("UP", "DOWN") and tf1h != direction:
            return {**empty, "Early Signal": "WAIT — 1H CONFLICT", "Early Direction": direction,
                    "Early Signal Reason": f"Early direction {direction}; 1H trend is opposite ({d1h})."}

        # Evidence score is a weighted screening score, not a win probability.
        parts, weights = [], []
        if one_is_candidate:
            parts.append(one_score); weights.append(0.35)
        if five_is_candidate:
            parts.append(max(0.0, min(100.0, five_score))); weights.append(0.35)
        score15 = min(100.0, abs(_safe_num(row.get("15M Score"), 0.0)) * 2.0)
        parts.append(score15); weights.append(0.20)
        if d1h in ("BUY", "SELL"):
            score1h = min(100.0, abs(_safe_num(row.get("1H Score"), 0.0)) * 2.0)
            parts.append(score1h); weights.append(0.10)
        evidence = round(sum(v*w for v, w in zip(parts, weights)) / sum(weights)) if weights else 0

        is_1m_confirmed = "CONFIRMED BREAKOUT" in one_status
        if is_1m_confirmed and one_is_candidate and five_is_candidate:
            signal = "BREAKOUT CONFIRMED — RETEST CHECK"
            confirmation = "1M BREAKOUT + 5M + 15M ALIGNED"
        elif one_is_candidate and five_is_candidate:
            signal = "EARLY WATCH — MULTI-TF ALIGNED"
            confirmation = "1M + 5M + 15M ALIGNED"
        elif five_is_candidate:
            signal = "EARLY WATCH — 5M ONLY"
            confirmation = "5M + 15M ALIGNED; 1M NOT AVAILABLE/READY"
        else:
            signal = "EARLY WATCH — 1M ONLY"
            confirmation = "1M SETUP; WAIT FOR 5M CONFIRMATION"
        reason = (
            f"Direction {direction}; 1M={one_status} ({one_score:.0f}), "
            f"5M={five_status} ({five_score:.0f}), 15M={d15}, 1H={d1h}. "
            "Early Evidence Score is a screening score, not probability."
        )
        return {
            "Early Signal": signal, "Early Direction": direction,
            "Early Evidence Score": int(evidence), "Early Confirmation": confirmation,
            "Early Signal Reason": reason,
            "Early Signal Time (IST)": (
                row.get("1M Candle Close (IST)") if one_is_candidate and row.get("1M Candle Close (IST)")
                else row.get("5M Candle Close (IST)")
            ),
        }
    except Exception as exc:
        return {**empty, "Early Signal": "WAIT — CALCULATION ERROR",
                "Early Signal Reason": str(exc)[:160]}

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
                    since_close = age_min - float(minutes)
                    allowed = {"5M": 20, "15M": 30, "1H": 90}.get(tf, minutes + 30)
                    elapsed_from_open = (now_local - (now_local.normalize() + SESSION_OPEN)).total_seconds() / 60.0
                    before_first_bar = (
                        stamp.date() < now_local.date()
                        and 0 <= elapsed_from_open <= (minutes + 15)
                    )
                    if since_close > (allowed + 0.5) and not before_first_bar:
                        issues.append(f"Latest candle is stale ({since_close:.0f} min since close)")
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
    """Describe 15M market regime using multi-day ADX with overnight gaps neutralized."""
    empty = {
        "regime": "UNKNOWN", "strength": 0,
        "reason": "Need at least 30 valid 15M candles",
        "adx": None, "atr_pct": None, "bb_width_pct": None,
    }
    if df is None or not isinstance(df, pd.DataFrame) or len(df) < 30:
        return empty
    try:
        ordered = df.sort_values("Time").drop_duplicates("Time", keep="last").copy()
        ordered["Time"] = pd.to_datetime(ordered["Time"], errors="coerce")
        ordered = ordered.dropna(subset=["Time"]).tail(120).reset_index(drop=True)
        if len(ordered) < 30 or not {"Open", "High", "Low", "Close"}.issubset(ordered.columns):
            return empty

        # Multi-day history supplies Wilder ADX warm-up. Keep only a short
        # first-hour guard so the regime is not hidden for most of the session.
        latest_day = ordered["Time"].iloc[-1].date()
        latest_session_count = int((ordered["Time"].dt.date == latest_day).sum())
        if latest_session_count < 4:
            warm = empty.copy()
            warm["regime"] = "SESSION WARM-UP"
            warm["reason"] = f"Need 4 same-session 15M candles; have {latest_session_count}"
            return warm

        o = pd.to_numeric(ordered["Open"], errors="coerce")
        h = pd.to_numeric(ordered["High"], errors="coerce")
        l = pd.to_numeric(ordered["Low"], errors="coerce")
        c = pd.to_numeric(ordered["Close"], errors="coerce")
        if any(x.isna().any() for x in (o, h, l, c)) or c.iloc[-1] <= 0:
            return empty

        day = ordered["Time"].dt.date
        first = day.ne(day.shift(1))
        up_move = h.diff().where(~first, 0.0)
        down_move = (-l.diff()).where(~first, 0.0)
        plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
        minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
        prev_close = c.shift(1).where(~first, o)
        tr = pd.concat([(h-l), (h-prev_close).abs(), (l-prev_close).abs()], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
        plus_di = 100 * plus_dm.ewm(alpha=1/14, adjust=False, min_periods=14).mean() / atr.replace(0, np.nan)
        minus_di = 100 * minus_dm.ewm(alpha=1/14, adjust=False, min_periods=14).mean() / atr.replace(0, np.nan)
        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
        adx_value = dx.ewm(alpha=1/14, adjust=False, min_periods=14).mean().iloc[-1]
        if pd.isna(adx_value):
            return {**empty, "regime": "INDICATOR WARM-UP", "reason": "ADX needs more valid multi-day 15M history"}
        adx = _safe_num(adx_value, 0.0)
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
        elif adx < 18 and bb_width < 2.5:
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
            open_ = frame_15m["Open"].astype(float)
            day = pd.to_datetime(frame_15m["Time"]).dt.date
            first_bar = day.ne(day.shift(1))
            previous_close = close.shift(1).where(~first_bar, open_)
            true_range = pd.concat(
                [(high - low), (high - previous_close).abs(), (low - previous_close).abs()],
                axis=1,
            ).max(axis=1)
            # Wilder smoothing matches the regime/ADX ATR implementation.
            atr_value = true_range.ewm(alpha=1/14, adjust=False, min_periods=14).mean().iloc[-1]
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
    row["1M Candle Close (IST)"] = _last_candle_close_time(payload.get("early_1m", pd.DataFrame()), 1)
    row.update(_prebig_5m_analysis(frame_5m))
    row.update(_advanced_big_movement_analysis(frame_5m, frame_15m, frame_1h))
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
    # New early-decision layer is additive; legacy Final Signal and Action Signal stay untouched.
    row.update(_early_signal_decision(row))
    for tf in TIMEFRAMES:
        liquidity = _liquidity_levels_and_sweeps(frames.get(tf, pd.DataFrame()), timeframe_minutes=TIMEFRAMES[tf][1])
        for key, value in liquidity.items():
            row[f"{tf} {key}"] = value
    # Keep simple unprefixed fields for the main report, using 15M as the reference.
    row.update(_liquidity_levels_and_sweeps(frame_15m, timeframe_minutes=15))
    row.update(_extra_formulas(frame_5m, row))
    row.update(_breakout_breakdown_signal(frame_15m))
    row.update(_multi_tf_breakout(frames))
    main_break = row.get("Breakout Signal", "WAIT")
    up_retest = _breakout_retest_confirmation(frame_15m, "UP")
    down_retest = _breakout_retest_confirmation(frame_15m, "DOWN")
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
    advanced_status = str(row.get("Advanced Big Move", "WAIT")).upper()
    if advanced_status == "BUY CONFIRMED":
        row["Action Signal"] = "BUY CONFIRMED"
    elif advanced_status == "SELL CONFIRMED":
        row["Action Signal"] = "SELL CONFIRMED"
    elif advanced_status in ("BREAKOUT DETECTED", "RETEST WAITING", "RETEST HELD — 15M WAIT"):
        row["Action Signal"] = "BREAKOUT DETECTED — WAIT RETEST / 15M"
    elif advanced_status in ("BREAKDOWN DETECTED", "RETEST REJECTED — 15M WAIT"):
        row["Action Signal"] = "BREAKDOWN DETECTED — WAIT RETEST / 15M"
    elif advanced_status == "FALSE BREAKOUT RISK":
        row["Action Signal"] = "FALSE BREAKOUT — WAIT"
    elif row.get("Multi-TF Breakout") == "BREAKOUT CONFIRMED":
        row["Action Signal"] = "BREAKOUT DETECTED — WAIT RETEST / 15M"
    elif row.get("Multi-TF Breakout") == "BREAKDOWN CONFIRMED":
        row["Action Signal"] = "BREAKDOWN DETECTED — WAIT RETEST / 15M"
    elif row.get("Upside Sweep") == "YES":
        row["Action Signal"] = "SWEEP UP"
    elif row.get("Downside Sweep") == "YES":
        row["Action Signal"] = "SWEEP DOWN"
    elif final_signal in ("BUY", "SELL"):
        row["Action Signal"] = final_signal
    else:
        row["Action Signal"] = "WAIT"
    if str(row.get("Data Quality", "PASS")).strip().upper() != "PASS":
        row["Advanced Big Move"] = "NO DATA"
        row["Action Signal"] = "NO DATA"
        row["Early Signal"] = "WAIT — DATA QUALITY FAIL"
        row["Early Direction"] = "WAIT"
        row["Early Confirmation"] = "NOT CONFIRMED"
        row["Early Signal Reason"] = str(row.get("Data Quality Reason", "Data Quality gate failed"))
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


def _fetch_interval(tickers, interval, minutes, period, retries=0):
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
        stamps = pd.to_datetime(frame["Time"], errors="coerce").dropna().sort_values().drop_duplicates()
        # Illiquid stocks may legitimately have multi-hour intraday gaps. Check
        # only for wholly missing recent weekdays; holidays merely cause a small
        # harmless 5-day overlap refresh.
        days = sorted({stamp.date() for stamp in stamps})
        if days:
            present_days = set(days)
            recent_weekdays = [
                day for day in pd.bdate_range(days[0], days[-1]).date
                if (now.date() - day).days <= 5
            ]
            if any(day not in present_days for day in recent_weekdays):
                return "5d"
        if gap_days <= 1:
            return "2d"
        if gap_days <= 4:
            return "5d"
        return full_period
    except Exception:
        return full_period


def _scan_batch(batch, period, include_early=True):
    tickers = [ticker for _, ticker in batch]
    per_tf, fresh_per_tf, problems = {}, {}, {ticker: [] for ticker in tickers}
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
        fresh_frames, errors = {}, {}
        if stale_tickers:
            # Do not let one old/missing cache force every symbol to download 30/60 days.
            period_groups = {}
            for ticker in stale_tickers:
                required_period = _cache_gap_fetch_period(cached[ticker], period)
                period_groups.setdefault(required_period, []).append(ticker)
            for required_period, group_tickers in period_groups.items():
                group_frames, group_errors = _fetch_interval(group_tickers, interval, minutes, required_period)
                fresh_frames.update(group_frames)
                errors.update(group_errors)
        frames = {}
        for ticker in tickers:
            frames[ticker] = _merge_candle_frames(cached[ticker], fresh_frames.get(ticker))
            if frames[ticker].empty:
                problems[ticker].append(f"{label}: {errors.get(ticker, 'No cached or fresh data')}")
        per_tf[label] = frames
        fresh_per_tf[label] = fresh_frames

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
        early_fresh, early_errors = {}, {}
        if early_stale:
            early_period_groups = {}
            for ticker in early_stale:
                required_period = _cache_gap_fetch_period(early_cached[ticker], "7d")
                early_period_groups.setdefault(required_period, []).append(ticker)
            for required_period, group_tickers in early_period_groups.items():
                group_frames, group_errors = _fetch_interval(group_tickers, "1m", 1, required_period)
                early_fresh.update(group_frames)
                early_errors.update(group_errors)
        early_frames = {
            ticker: _merge_candle_frames(early_cached[ticker], early_fresh.get(ticker))
            for ticker in tickers
        }
        per_tf["1M"] = early_frames
        fresh_per_tf["1M"] = early_fresh
        for ticker in tickers:
            if early_frames[ticker].empty:
                problems[ticker].append(f"1M: {early_errors.get(ticker, 'No 1M data')}")
    else:
        per_tf["1M"] = {ticker: pd.DataFrame() for ticker in tickers}
        fresh_per_tf["1M"] = {}

    try:
        _save_candle_memory(batch, fresh_per_tf)
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
    if result.empty:
        return result
    if "Final Signal" not in result.columns:
        result["Final Signal"] = "NO DATA"
    if "Confirmation Strength %" not in result.columns:
        result["Confirmation Strength %"] = 0
    if "Symbol" not in result.columns:
        result["Symbol"] = ""
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

    # Direct trend has priority; if it is WAIT, use that timeframe's Pre-Big agent.
    tfs = []
    for tf in ("5M", "15M", "1H"):
        direct = str(row.get(tf, "NO DATA") or "NO DATA").upper()
        pre = str(row.get(f"{tf} Pre-Big_Movement", "WAIT") or "WAIT").upper()
        tfs.append(direct if direct in ("BUY", "SELL") else pre)
    aligned = sum(x in (expected, f"PRE-BIG {expected}") for x in tfs)
    opposing = sum(x in (("SELL", "PRE-BIG SELL") if expected == "BUY"
                         else ("BUY", "PRE-BIG BUY")) for x in tfs)
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
    """Summarise direct trend and per-timeframe Pre-Big Movement agent votes."""
    directions = []
    for tf in ("5M", "15M", "1H"):
        direct = str(row.get(tf, "NO DATA") or "NO DATA").upper()
        pre = str(row.get(f"{tf} Pre-Big_Movement", "WAIT") or "WAIT").upper()
        if direct in ("BUY", "SELL"):
            directions.append(direct)
        elif pre in ("PRE-BIG BUY", "PRE-BIG SELL"):
            directions.append(pre)
        else:
            directions.append(direct)

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
    elif buy_votes == 0 and sell_votes == 0 and all(x in ("WAIT", "NO DATA") for x in directions):
        movement = "SIDEWAYS" if all(x == "WAIT" for x in directions) else "NO DATA"
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


def _expire_stale_scan_jobs(max_age_hours=4):
    """Release jobs from an earlier day or jobs with no progress for more than four hours."""
    now = pd.Timestamp.now(tz=IST)
    cutoff = now - pd.Timedelta(hours=float(max_age_hours))
    with _memory_connect() as conn:
        rows = conn.execute(
            "SELECT job_id,status,updated_at FROM scan_jobs WHERE status IN ('RUNNING','PAUSED','DONE')"
        ).fetchall()
        for job_id, status, updated_text in rows:
            stamp = _as_ist_timestamp(updated_text)
            stale = stamp is None or stamp.date() != now.date() or stamp < cutoff
            if not stale:
                continue
            if status == "DONE":
                # Preserve completed rows so the regular restore path can still load them.
                conn.execute("UPDATE scan_jobs SET status='FINALIZED' WHERE job_id=? AND status='DONE'", (job_id,))
            else:
                conn.execute(
                    "UPDATE scan_jobs SET status='CANCELLED',updated_at=?,error=? WHERE job_id=? AND status IN ('RUNNING','PAUSED')",
                    (now.strftime("%Y-%m-%d %H:%M:%S %Z"), "Auto-expired: previous trading day or no progress for over 4 hours.", job_id),
                )


def _prune_scan_jobs(keep=3):
    """Expire stalled jobs, then retain the latest completed result and short history."""
    _expire_stale_scan_jobs()
    keep = max(1, int(keep))
    with _memory_connect() as conn:
        old_ids = [row[0] for row in conn.execute(
            """SELECT job_id FROM scan_jobs WHERE status IN ('FINALIZED','CANCELLED')
               ORDER BY created_at DESC LIMIT -1 OFFSET ?""", (keep,)
        ).fetchall()]
        old_done = [row[0] for row in conn.execute(
            """SELECT job_id FROM scan_jobs WHERE status='DONE'
               ORDER BY created_at DESC LIMIT -1 OFFSET 1"""
        ).fetchall()]
        for old_id in set(old_ids + old_done):
            conn.execute("DELETE FROM scan_job_rows WHERE job_id=?", (old_id,))
            conn.execute("DELETE FROM scan_jobs WHERE job_id=?", (old_id,))


def _scan_job_create(config):
    """Create a durable scan job and return its unique ID."""
    _prune_scan_jobs(keep=3)
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
        _expire_stale_scan_jobs()
        with _memory_connect() as conn:
            row = conn.execute(
                """SELECT job_id FROM scan_jobs
                   WHERE status IN ('RUNNING','PAUSED','DONE')
                   ORDER BY updated_at DESC, created_at DESC LIMIT 1"""
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
    """Background scanner with one retry; never convert a batch exception into dead tickers."""
    job = _scan_job_get(job_id)
    if not job:
        return
    config = job["config"]
    symbols = [(str(x[0]), str(x[1])) for x in config.get("symbols", [])]
    batch_size = max(10, min(int(config.get("batch_size", 50)), 100))
    period = str(config.get("period", "30d"))
    include_early = bool(config.get("include_early", False))
    try:
        with _memory_connect() as conn:
            done_rows = conn.execute("SELECT symbol FROM scan_job_rows WHERE job_id=?", (job_id,)).fetchall()
        completed_symbols = {x[0] for x in done_rows}
        pending = [x for x in symbols if x[0] not in completed_symbols]

        for start in range(0, len(pending), batch_size):
            current_job = _scan_job_get(job_id)
            if not current_job or current_job["status"] == "CANCELLED":
                return
            batch = pending[start:start + batch_size]
            batch_rows = None
            batch_error = None
            # First attempt plus one complete retry on exception or zero-candle rows.
            for attempt in range(2):
                try:
                    rows = _scan_batch(batch, period, include_early=include_early)
                    if not isinstance(rows, list) or len(rows) != len(batch):
                        raise RuntimeError(f"Yahoo batch returned {len(rows) if isinstance(rows, list) else 'invalid'} rows for {len(batch)} symbols")
                    batch_rows = rows
                    bad = [i for i, row in enumerate(batch_rows)
                           if int(row.get("5M Candles", 0) or 0) == 0
                           and int(row.get("15M Candles", 0) or 0) == 0
                           and int(row.get("1H Candles", 0) or 0) == 0]
                    if not bad or attempt == 1:
                        break
                    time.sleep(1.0)
                    retry_rows = _scan_batch([batch[i] for i in bad], period, include_early=include_early)
                    if not isinstance(retry_rows, list) or len(retry_rows) != len(bad):
                        raise RuntimeError("Yahoo retry returned an incomplete batch")
                    for i, retry_row in zip(bad, retry_rows):
                        batch_rows[i] = retry_row
                    break
                except Exception as exc:
                    batch_error = exc
                    if attempt == 0:
                        time.sleep(1.0)
                        continue
            if batch_rows is None:
                # Keep all symbols unresolved; a true exception is not a delisted ticker.
                batch_rows = [_no_data_row(sym, ticker, f"Yahoo download failed: {str(batch_error)[:180]}")
                              for sym, ticker in batch]
                unresolved_indices = list(range(len(batch)))
            else:
                unresolved_indices = [i for i, row in enumerate(batch_rows)
                                      if int(row.get("5M Candles", 0) or 0) == 0
                                      and int(row.get("15M Candles", 0) or 0) == 0
                                      and int(row.get("1H Candles", 0) or 0) == 0]
                # If any unresolved rows remain, probe Yahoo directly, bypassing TTL cache.
                # Healthy Yahoo => these are per-symbol misses; unhealthy Yahoo => pause and retry later.
                if unresolved_indices and _yahoo_is_healthy():
                    unresolved_indices = []

            current_job = _scan_job_get(job_id)
            if not current_job or current_job["status"] == "CANCELLED":
                return
            saved_at = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S %Z")
            unresolved_set = set(unresolved_indices)
            payload_rows = [
                (job_id, str(row.get("Symbol", symbol)), str(row.get("Yahoo Symbol", ticker)),
                 sqlite3.Binary(pickle.dumps(row, protocol=pickle.HIGHEST_PROTOCOL)), saved_at)
                for i, ((symbol, ticker), row) in enumerate(zip(batch, batch_rows))
                if i not in unresolved_set
            ]
            with _memory_connect() as conn:
                conn.executemany(
                    """INSERT INTO scan_job_rows(job_id,symbol,ticker,payload,saved_at)
                       VALUES (?,?,?,?,?) ON CONFLICT(job_id,symbol) DO UPDATE SET
                       ticker=excluded.ticker,payload=excluded.payload,saved_at=excluded.saved_at""",
                    payload_rows,
                )
                total_done = conn.execute("SELECT COUNT(*) FROM scan_job_rows WHERE job_id=?", (job_id,)).fetchone()[0]
                conn.execute(
                    """UPDATE scan_jobs SET status='RUNNING',completed_symbols=?,updated_at=?,error=NULL
                       WHERE job_id=? AND status!='CANCELLED'""",
                    (int(total_done), saved_at, job_id),
                )
            # Capture each healthy batch immediately. This prevents the first
            # NSE/F&O batches from ageing out while the rest of the universe scans.
            try:
                capture_rows = [
                    row for i, row in enumerate(batch_rows)
                    if i not in unresolved_set
                ]
                if capture_rows:
                    _capture_all_dashboard_events(
                        pd.DataFrame(capture_rows),
                        reference_time=_as_ist_timestamp(saved_at),
                    )
            except Exception:
                # Dashboard ledgers must never stop the core scan worker.
                pass
            if unresolved_indices:
                paused_at = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S %Z")
                with _memory_connect() as conn:
                    conn.execute(
                        "UPDATE scan_jobs SET status='PAUSED',updated_at=?,error=? WHERE job_id=? AND status!='CANCELLED'",
                        (paused_at, f"Yahoo download unresolved for {len(unresolved_indices)} symbols after retry; no permanent NO DATA rows saved. Press RESUME.", job_id),
                    )
                return

        # Third-pass zero-candle refetch skipped to avoid repeated Yahoo requests.
        finished_at = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S %Z")
        with _memory_connect() as conn:
            total_done = conn.execute("SELECT COUNT(*) FROM scan_job_rows WHERE job_id=?", (job_id,)).fetchone()[0]
            conn.execute(
                """UPDATE scan_jobs SET status='DONE',completed_symbols=?,updated_at=?,error=NULL
                   WHERE job_id=? AND status!='CANCELLED'""",
                (int(total_done), finished_at, job_id),
            )
    except Exception as exc:
        paused_at = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S %Z")
        try:
            with _memory_connect() as conn:
                conn.execute("UPDATE scan_jobs SET status='PAUSED',updated_at=?,error=? WHERE job_id=? AND status!='CANCELLED'",
                             (paused_at, str(exc)[:1000], job_id))
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
        with _memory_connect() as conn:
            conn.execute(
                "UPDATE scan_jobs SET status='RUNNING',updated_at=?,error=NULL WHERE job_id=? AND status IN ('RUNNING','PAUSED')",
                (datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S %Z"), job_id),
            )
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
    session_key = (pd.to_datetime(d["Time"], errors="coerce", utc=True).dt.tz_convert(IST).dt.date
                   if "Time" in d.columns else pd.Series(0, index=d.index))
    prev = c.groupby(session_key).shift(1)  # first bar each session uses High-Low, not overnight gap
    tr = pd.concat([(h-l), (h-prev).abs(), (l-prev).abs()], axis=1).max(axis=1)
    atr = tr.rolling(14, min_periods=7).mean()
    avg_vol = v.shift(1).rolling(20, min_periods=8).mean()
    ema9 = c.ewm(span=9, adjust=False).mean()
    ema21 = c.ewm(span=21, adjust=False).mean()
    typical = (h + l + c) / 3
    cum_pv = (typical * v).groupby(session_key).cumsum()
    cum_v = v.groupby(session_key).cumsum().replace(0, np.nan)
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
                d = pd.DataFrame(rows, columns=["Time", "Open", "High", "Low", "Close", "Volume"])
                d["Time"] = pd.to_datetime(d["Time"], errors="coerce", utc=True)
                feat = _real_ai_feature_frame(d)
                # Forward horizon stays inside one NSE session; no overnight label leakage.
                session_key = d["Time"].dt.tz_convert(IST).dt.date
                future_close = d["Close"].groupby(session_key).shift(-3)
                future_time = d["Time"].groupby(session_key).shift(-3)
                future_return = future_close / d["Close"] - 1
                atr_pct = feat["AI ATR %"] / 100.0
                deadband = np.maximum(0.0010, 0.80 * atr_pct * math.sqrt(3.0))
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


def _load_persisted_ai_brain():
    """Load the most recently trained local model bundle, if present."""
    try:
        model_path = Path(__file__).with_name("ai_multi_agent_brain.pkl")
        if model_path.exists():
            with model_path.open("rb") as fh:
                bundle = pickle.load(fh)
            if (isinstance(bundle, dict) and "model" in bundle and "metrics" in bundle
                    and bundle.get("features") == list(AI_FEATURE_COLUMNS)):
                return bundle
    except Exception:
        return None
    return None


def _train_real_ai_brain(force=False):
    """Train a time-ordered RandomForest model; optional sklearn dependency."""
    if not force and st.session_state.get("ma_real_ai_model"):
        current_bundle = st.session_state["ma_real_ai_model"]
        if (isinstance(current_bundle, dict)
                and current_bundle.get("features") == list(AI_FEATURE_COLUMNS)):
            return current_bundle, None
        st.session_state["ma_real_ai_model"] = None
    if not force:
        persisted = _load_persisted_ai_brain()
        if persisted:
            st.session_state["ma_real_ai_model"] = persisted
            return persisted, None
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
    # Three expanding-window, chronological validation folds. Each fold purges
    # examples whose 3-bar target horizon overlaps the validation start.
    wf_scores = []
    time_values = pd.to_datetime(data["__time"], utc=True)
    target_values = pd.to_datetime(data["__target_time"], utc=True)
    n_rows = len(data)
    for train_fraction, test_end_fraction in ((0.50, 0.65), (0.65, 0.80), (0.80, 0.95)):
        start_i = int(n_rows * train_fraction)
        end_i = int(n_rows * test_end_fraction)
        if end_i <= start_i + 50:
            continue
        fold_start = time_values.iloc[start_i]
        fold_end = time_values.iloc[end_i]
        fold_train = (time_values < fold_start) & (target_values < fold_start)
        fold_test = (time_values >= fold_start) & (time_values < fold_end)
        if int(fold_train.sum()) < 300 or int(fold_test.sum()) < 100:
            continue
        if y.loc[fold_train].nunique() < 3 or y.loc[fold_test].nunique() < 2:
            continue
        fold_model = RandomForestClassifier(
            n_estimators=120, max_depth=8, min_samples_leaf=5,
            class_weight="balanced_subsample", random_state=42, n_jobs=2,
        )
        fold_model.fit(X.loc[fold_train], y.loc[fold_train])
        fold_pred = fold_model.predict(X.loc[fold_test])
        wf_scores.append(float(balanced_accuracy_score(y.loc[fold_test], fold_pred)))
    metrics = {
        "samples": int(len(data)), "train_samples": int(len(X_train)),
        "test_samples": int(len(X_test)),
        "accuracy": float(accuracy_score(y_test, predicted)),
        "balanced_accuracy": float(balanced_accuracy_score(y_test, predicted)),
        "walk_forward_balanced_accuracy": float(np.mean(wf_scores)) if wf_scores else None,
        "walk_forward_folds": len(wf_scores),
        "trained_at": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST"),
    }
    bundle = {
        "model": model,
        "metrics": metrics,
        "features": list(AI_FEATURE_COLUMNS),
    }
    try:
        model_path = Path(__file__).with_name("ai_multi_agent_brain.pkl")
        temp_path = model_path.with_suffix(".pkl.tmp")
        with temp_path.open("wb") as fh:
            pickle.dump(bundle, fh, protocol=pickle.HIGHEST_PROTOCOL)
        temp_path.replace(model_path)
    except Exception as exc:
        metrics["persistence_warning"] = str(exc)[:180]
    return bundle, None


def _apply_real_ai_brain(result):
    """Attach model estimates in one batch; never overwrite the 17-agent signal."""
    result = result.copy()
    # Clear any error text left by a previous failed prediction run.
    if "AI Prediction Error" in result.columns:
        result = result.drop(columns=["AI Prediction Error"])
    defaults = {
        "AI Direction": "NOT TRAINED", "AI Up Model Estimate %": None,
        "AI Down Model Estimate %": None, "AI Sideways Model Estimate %": None,
        "AI Brain Signal": "MODEL NOT TRAINED", "AI Model Status": "NOT TRAINED",
    }
    for col, default in defaults.items():
        # Clear old predictions before applying the currently loaded model.
        result[col] = default
    bundle = st.session_state.get("ma_real_ai_model")
    if (not isinstance(bundle, dict)
            or bundle.get("features") != list(AI_FEATURE_COLUMNS)
            or not all(c in result.columns for c in AI_FEATURE_COLUMNS)):
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
        result.loc[result.index[row_indices], "AI Down Model Estimate %"] = np.round(down * 100, 1)
        result.loc[result.index[row_indices], "AI Sideways Model Estimate %"] = np.round(sideways * 100, 1)
        result.loc[result.index[row_indices], "AI Up Model Estimate %"] = np.round(up * 100, 1)
        result.loc[result.index[row_indices], "AI Model Status"] = "TRAINED"

        technical = result["Final Signal"].fillna("WAIT").astype(str).str.upper().to_numpy()
        metrics = bundle.get("metrics", {})
        balanced = _safe_num(metrics.get("walk_forward_balanced_accuracy", metrics.get("balanced_accuracy")), 0.0)
        reliable = balanced >= 0.55 and int(metrics.get("walk_forward_folds", 0) or 0) >= 3
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


def _build_excel(result, stamp):
    """Build the complete dashboard + scanner workbook from the current result DataFrame."""
    try:
        from openpyxl.styles import Font, PatternFill, Alignment
        from openpyxl.formatting.rule import CellIsRule
        from openpyxl.chart import BarChart, Reference

        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            result.to_excel(writer, index=False, sheet_name="AI Scanner")
            counts = result.get("Action Signal", pd.Series(dtype=str)).fillna("WAIT").value_counts()
            dash_rows = [
                ["NSE AI PRO V18 — ADVANCED BIG MOVEMENT DASHBOARD", ""],
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
            pd.DataFrame(dash_rows, columns=["Metric", "Value"]).to_excel(
                writer, index=False, header=False, sheet_name="Dashboard", startrow=0
            )
            top_cols = [c for c in [
                "Symbol", "Action Signal", "Final Signal", "Best Stock Score",
                "Confirmation Strength %", "Last Price", "Buy-side Liquidity", "Buy-side Liquidity Time (IST)",
                "Sell-side Liquidity", "Sell-side Liquidity Time (IST)", "Upside Sweep", "Upside Sweep Time (IST)",
                "Downside Sweep", "Downside Sweep Time (IST)", "Breakout Signal",
                "Multi-TF Breakout", "Breakout Retest", "AI Direction",
                "AI Up Model Estimate %", "AI Down Model Estimate %", "AI Brain Signal",
            ] if c in result.columns]
            top = result.copy()
            if "Best Stock Filter" in top.columns:
                top = top[top["Best Stock Filter"] == "PASS"]
            if "Best Stock Score" in top.columns:
                top = top.sort_values("Best Stock Score", ascending=False)
            top[top_cols].head(15).to_excel(
                writer, index=False, sheet_name="Dashboard", startrow=len(dash_rows)+1
            )

            wb = writer.book
            ws = wb["Dashboard"]
            ws.freeze_panes = "A3"
            ws.column_dimensions["A"].width = 30
            ws.column_dimensions["B"].width = 34
            for col in range(3, max(ws.max_column, 2) + 1):
                ws.column_dimensions[ws.cell(1, col).column_letter].width = 22
            ws.merge_cells("A1:B1")
            ws["A1"].font = Font(bold=True, size=16, color="FFFFFF")
            ws["A1"].fill = PatternFill("solid", fgColor="17365D")
            ws["A1"].alignment = Alignment(horizontal="center")
            ws[2][0].font = Font(bold=True, color="17365D")
            ws[3][0].font = Font(bold=True, color="17365D")
            for r in range(3, 11):
                ws.cell(r, 1).font = Font(bold=True)
            header_row = len(dash_rows) + 2
            for cell in ws[header_row]:
                cell.font = Font(bold=True, color="FFFFFF")
                cell.fill = PatternFill("solid", fgColor="305496")
            for row_cells in ws.iter_rows(min_row=header_row + 1):
                for cell in row_cells:
                    cell.alignment = Alignment(vertical="center")

            chart = BarChart()
            chart.type = "bar"
            chart.style = 10
            chart.title = "Signal Counts"
            chart.x_axis.title = "Signal"
            chart.y_axis.title = "Stocks"
            chart.x_axis.delete = False
            chart.y_axis.delete = False
            chart.height, chart.width = 7, 12
            chart.add_data(Reference(ws, min_col=2, min_row=4, max_row=10), titles_from_data=False)
            chart.set_categories(Reference(ws, min_col=1, min_row=4, max_row=10))
            # Anchored to the right of the top-stocks table (columns A-M) so it never covers it.
            ws.add_chart(chart, "O2")

            report = wb["AI Scanner"]
            report.freeze_panes = "A2"
            last_col = report.cell(1, report.max_column).column_letter
            report.auto_filter.ref = f"A1:{last_col}{max(report.max_row, 1)}"
            for cell in report[1]:
                cell.font = Font(bold=True, color="FFFFFF")
                cell.fill = PatternFill("solid", fgColor="17365D")
                cell.alignment = Alignment(wrap_text=True, vertical="center")
            report.row_dimensions[1].height = 32
            for col_cells in report.columns:
                letter = col_cells[0].column_letter
                header = str(col_cells[0].value or "")
                report.column_dimensions[letter].width = min(max(len(header) + 2, 13), 28)
            headers = {str(c.value): c.column for c in report[1]}
            for key in ("Action Signal", "Final Signal", "Breakout Signal", "Multi-TF Breakout"):
                if key in headers:
                    col_letter = report.cell(1, headers[key]).column_letter
                    report.conditional_formatting.add(
                        f"{col_letter}2:{col_letter}{report.max_row}",
                        CellIsRule(operator="equal", formula=['"BUY"'],
                                   fill=PatternFill("solid", fgColor="C6EFCE")),
                    )
                    report.conditional_formatting.add(
                        f"{col_letter}2:{col_letter}{report.max_row}",
                        CellIsRule(operator="equal", formula=['"SELL"'],
                                   fill=PatternFill("solid", fgColor="F4CCCC")),
                    )
            wb._sheets = [wb["Dashboard"], wb["AI Scanner"]]
        return buffer.getvalue(), ""
    except Exception as exc:
        return None, str(exc)



def _prescan_supabase_config():
    """Read Supabase credentials from Streamlit secrets or environment."""
    url = str(os.getenv("SUPABASE_URL", "") or "").strip().rstrip("/")
    key = str(os.getenv("SUPABASE_SERVICE_ROLE_KEY", "") or "").strip()
    if not url or not key:
        try:
            url = url or str(st.secrets.get("SUPABASE_URL", "")).strip().rstrip("/")
            key = key or str(st.secrets.get("SUPABASE_SERVICE_ROLE_KEY", "")).strip()
        except Exception:
            pass
    if not url or not key:
        raise RuntimeError("Supabase credentials missing. Configure SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in Streamlit Secrets.")
    headers = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    return url, key, headers


def _merge_remote_candle_cache(url, headers):
    """Merge cloud candle rows into the local DB without replacing local ledgers."""
    bucket = str(os.getenv("SUPABASE_STORAGE_BUCKET", "ai-scanner-memory"))
    resp = requests.get(
        f"{url}/storage/v1/object/{bucket}/ai_multi_agent_candle_memory.sqlite3",
        headers=headers, timeout=90,
    )
    if not (resp.ok and resp.content):
        return 0
    tmp = MEMORY_DB_PATH.with_suffix(".remote.tmp")
    tmp.write_bytes(resp.content)
    merged = 0
    try:
        with _memory_connect() as conn:
            conn.execute("ATTACH DATABASE ? AS remote", (str(tmp),))
            try:
                before = conn.total_changes
                conn.execute("""INSERT OR REPLACE INTO candles
                    (symbol,timeframe,candle_time,open,high,low,close,volume,saved_at)
                    SELECT symbol,timeframe,candle_time,open,high,low,close,volume,saved_at
                    FROM remote.candles""")
                conn.execute("DELETE FROM candles WHERE timeframe='1H' AND substr(candle_time,12,5)='15:15'")
                merged = conn.total_changes - before
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.execute("DETACH DATABASE remote")
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                Path(str(tmp) + suffix).unlink()
            except Exception:
                pass
    return merged


def _load_latest_prescan_to_local_job(universe, batch_size=50, period="59d", depth_limit=10):
    """Download a completed cloud pre-scan and hydrate existing SQLite job rows.

    The normal V18 report-finalization path then adds its existing AI/18th-agent/UI
    logic. No technical-agent formulas are changed here.
    """
    url, _key, headers = _prescan_supabase_config()
    # Restore the same persistent candle cache used by the scheduled worker.
    # A missing cache is non-fatal: the saved report can still be loaded.
    try:
        _merge_remote_candle_cache(url, headers)
    except Exception:
        pass
    runs_url = f"{url}/rest/v1/prescan_runs"
    run_resp = requests.get(
        runs_url, headers=headers,
        params={"select": "id,status,universe,scheduled_type,started_at,completed_at,total_symbols,completed_symbols,symbols",
                "status": "eq.DONE", "universe": f"eq.{universe}",
                "order": "completed_at.desc", "limit": "1"}, timeout=30,
    )
    run_resp.raise_for_status()
    runs = run_resp.json()
    if not runs:
        raise RuntimeError(f"No completed background report found for universe: {universe}")
    run = runs[0]
    symbols = run.get("symbols") or []
    if not symbols:
        raise RuntimeError("The latest background report has no saved symbol list.")
    rows = []
    offset, page_size = 0, 1000
    while True:
        resp = requests.get(
            f"{url}/rest/v1/prescan_rows", headers={**headers, "Range-Unit": "items", "Range": f"{offset}-{offset + page_size - 1}"},
            params={"select": "symbol,ticker,payload", "scan_id": f"eq.{run['id']}", "order": "symbol.asc", "limit": str(page_size)},
            timeout=60,
        )
        resp.raise_for_status()
        page = resp.json()
        rows.extend(page)
        if len(page) < page_size:
            break
        offset += page_size
    payload_by_symbol = {str(item.get("symbol")): item.get("payload") for item in rows if isinstance(item.get("payload"), dict)}
    ordered_rows = [payload_by_symbol[str(item[0])] for item in symbols if str(item[0]) in payload_by_symbol]
    if not ordered_rows:
        raise RuntimeError("The background report exists, but no stock result rows could be loaded.")
    config = {
        "universe": universe, "source_symbols": len(symbols), "symbols": symbols,
        "batch_size": int(batch_size), "period": str(period), "include_early": False,
        "news_enabled": False, "news_limit": 50, "depth_limit": int(depth_limit),
        "prescan_run_id": run["id"], "prescan_completed_at": run.get("completed_at"),
    }
    job_id = _scan_job_create(config)
    saved_at = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S %Z")
    insert_rows = []
    for payload in ordered_rows:
        sym = str(payload.get("Symbol", ""))
        ticker = str(payload.get("Yahoo Symbol", ""))
        if sym and ticker:
            insert_rows.append((job_id, sym, ticker, sqlite3.Binary(pickle.dumps(payload, protocol=pickle.HIGHEST_PROTOCOL)), saved_at))
    with _memory_connect() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO scan_job_rows(job_id,symbol,ticker,payload,saved_at) VALUES (?,?,?,?,?)",
            insert_rows,
        )
        conn.execute(
            "UPDATE scan_jobs SET status='DONE',completed_symbols=?,updated_at=?,error=NULL WHERE job_id=?",
            (len(insert_rows), saved_at, job_id),
        )
    return job_id, run, len(insert_rows)

# =====================================================================
# V18 PATCH — paste this whole block into your scanner file ABOVE
# `def show_ai_multi_agent_scanner(...)`. It uses the names that already
# exist in your file (_memory_connect, _safe_num, _as_ist_timestamp, IST,
# TIMEFRAMES, _risk_levels_for_event, _is_fresh_signal_time, _stretch ...).
# Then apply the 4 small edits listed in the chat reply.
# =====================================================================

BIG_VOL_RVOL_MIN = 2.0        # big-volume threshold (x prior 20-bar average)
LIQ_NEAR_PCT = 0.5            # "near liquidity" = within 0.5% of the level
BIG_MOVE_MIN_SCORE = 50       # minimum screening score to cache a big-move stock

_PATCH_TABLES_READY = False
_PATCH_LOCK = threading.Lock()


def _ensure_patch_tables():
    """Create the persistent caches once (safe on old databases)."""
    global _PATCH_TABLES_READY
    if _PATCH_TABLES_READY:
        return
    with _PATCH_LOCK:
        if _PATCH_TABLES_READY:
            return
        with _memory_connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS big_move_events (
                event_key TEXT PRIMARY KEY, symbol TEXT NOT NULL, status TEXT NOT NULL,
                direction TEXT, score REAL, price REAL, range_high REAL, range_low REAL,
                rvol REAL, source TEXT, reason TEXT,
                first_seen_ist TEXT NOT NULL, last_seen_ist TEXT NOT NULL, candle_time_ist TEXT)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS liquidity_watch (
                symbol TEXT PRIMARY KEY, price REAL, buy_level REAL, buy_dist_pct REAL,
                sell_level REAL, sell_dist_pct REAL, state TEXT, bias TEXT,
                sweep_tf TEXT, sweep_time_ist TEXT, rvol REAL, updated_at_ist TEXT NOT NULL)""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_big_move_seen ON big_move_events(last_seen_ist)")
        _PATCH_TABLES_READY = True


def _today_text():
    return datetime.now(IST).strftime("%Y-%m-%d")


def _candle_is_recent(value, max_age_min, now=None):
    """True only for a completed candle from the reference day and within max_age_min."""
    stamp = _as_ist_timestamp(value)
    if stamp is None:
        return False
    now = _as_ist_timestamp(now) if now is not None else pd.Timestamp.now(tz=IST)
    if now is None:
        now = pd.Timestamp.now(tz=IST)
    if stamp.date() != now.date():
        return False
    age = (now - stamp).total_seconds() / 60.0
    return -2.0 <= age <= float(max_age_min)


# ---------------------------------------------------------------------
# NEW FORMULAS (additive; 17 agents untouched)
# ---------------------------------------------------------------------
_EXTRA_FORMULA_DEFAULTS = {
    "5M MACD State": "NO DATA", "5M MACD Hist": None,
    "5M BB Squeeze": "NO DATA", "5M BB Width Pctile": None,
    "5M ATR Expansion": None, "5M OBV Slope": None, "5M Volume Z": None,
    "Big Volume RVOL": None, "5M Turnover Cr": None, "Session Turnover Cr": None,
    "ORB State": "NO DATA", "Day High Dist %": None, "Day Low Dist %": None,
    "Gap %": None, "Big Volume Class": "NO DATA", "Big Volume Side": "WAIT",
    "Liq Buy Dist %": None, "Liq Sell Dist %": None, "Liquidity State": "NO DATA",
    "Big Move V2 Direction": "WAIT", "Big Move V2 Score": 0,
    "Big Move V2 Reason": "Need 35 completed 5M candles", "Formula Status": "NO DATA",
}


def _extra_formulas(frame_5m, row=None):
    """MACD, Bollinger squeeze, ATR expansion, OBV slope, volume z-score, turnover,
    opening-range state, day-high/low distance, gap %, liquidity distance and a
    Big Move V2 screening score. Scores are NOT probabilities."""
    out = dict(_EXTRA_FORMULA_DEFAULTS)
    row = row or {}
    req = ["Time", "Open", "High", "Low", "Close", "Volume"]
    try:
        if not isinstance(frame_5m, pd.DataFrame) or not set(req).issubset(frame_5m.columns):
            return out
        d = frame_5m[req].copy()
        d["Time"] = pd.to_datetime(d["Time"], errors="coerce")
        for col in req[1:]:
            d[col] = pd.to_numeric(d[col], errors="coerce")
        d = d.dropna().sort_values("Time").drop_duplicates("Time", keep="last").reset_index(drop=True)
        if len(d) < 35:
            out["Formula Status"] = f"NEED 35 5M CANDLES; HAVE {len(d)}"
            return out
        o, h, l, c, v = (d[k].astype(float) for k in ("Open", "High", "Low", "Close", "Volume"))
        price = float(c.iloc[-1])
        if price <= 0:
            return out
        day = d["Time"].dt.date
        last_day = day.iloc[-1]
        sess = d[day == last_day].reset_index(drop=True)
        prev = d[day < last_day]

        # 1) MACD (12,26,9) histogram
        macd = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
        hist = macd - macd.ewm(span=9, adjust=False).mean()
        hn, hp = float(hist.iloc[-1]), float(hist.iloc[-2])
        if hn > 0:
            macd_state = "BULL RISING" if hn > hp else "BULL FADING"
        else:
            macd_state = "BEAR FALLING" if hn < hp else "BEAR FADING"

        # 2) Bollinger squeeze (width in lowest 20% of last 60 bars)
        mid = c.rolling(20).mean()
        sd = c.rolling(20).std(ddof=0)
        width = (4 * sd / mid.replace(0, np.nan) * 100).dropna().tail(60)
        pctile = float((width <= width.iloc[-1]).mean() * 100) if len(width) >= 20 else np.nan
        squeeze = bool(np.isfinite(pctile) and pctile <= 20)

        # 3) ATR expansion: ATR(5) / ATR(20)
        pc = c.shift(1)
        tr = pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
        atr20 = float(tr.tail(20).mean())
        atr_ratio = float(tr.tail(5).mean()) / atr20 if atr20 > 0 else np.nan

        # 4) OBV slope on the current session, normalised to -1..+1
        obv_slope = np.nan
        if len(sess) >= 8:
            sc, sv = sess["Close"].astype(float), sess["Volume"].astype(float)
            obv = (np.sign(sc.diff().fillna(0)) * sv).cumsum()
            n = min(7, len(sess) - 1)
            obv_slope = float(np.clip((obv.iloc[-1] - obv.iloc[-1 - n]) / max(sv.tail(n).sum(), 1.0), -1, 1))

        # 5) Volume z-score + RVOL against the previous 20 candles
        base = v.iloc[-21:-1]
        mu, sig = float(base.mean()), float(base.std(ddof=0))
        rvol = float(v.iloc[-1]) / mu if mu > 0 else 0.0
        vz = (float(v.iloc[-1]) - mu) / sig if sig > 0 else 0.0

        # 6) Turnover (INR crore)
        turn_last = price * float(v.iloc[-1]) / 1e7
        typ = (sess["High"] + sess["Low"] + sess["Close"]) / 3
        turn_sess = float((typ * sess["Volume"]).sum() / 1e7)

        # 7) Opening range (first 3 x 5M = 15 minutes)
        orb = "INSIDE OR"
        if len(sess) < 4:
            orb = "OR FORMING"
        else:
            orh, orl = float(sess["High"].iloc[:3].max()), float(sess["Low"].iloc[:3].min())
            orb = "ORB UP" if price > orh else "ORB DOWN" if price < orl else "INSIDE OR"

        # 8) Day high/low distance and gap %
        dh, dl = float(sess["High"].max()), float(sess["Low"].min())
        dist_high = (dh - price) / price * 100
        dist_low = (price - dl) / price * 100
        gap = np.nan
        if not prev.empty and len(sess):
            pclose = float(prev["Close"].iloc[-1])
            if pclose > 0:
                gap = (float(sess["Open"].iloc[0]) / pclose - 1) * 100

        # 9) Big-volume class (candle direction decides the side)
        side = "BUY" if c.iloc[-1] > o.iloc[-1] else "SELL" if c.iloc[-1] < o.iloc[-1] else "WAIT"
        if rvol >= 3.0 and vz >= 2.0:
            vclass = "EXTREME VOLUME"
        elif rvol >= BIG_VOL_RVOL_MIN:
            vclass = "BIG VOLUME"
        elif rvol >= 1.5:
            vclass = "RISING VOLUME"
        else:
            vclass = "NORMAL"

        # 10) Liquidity distance (15M levels vs latest 5M close)
        buy = _safe_num(row.get("15M Buy-side Liquidity"), np.nan)
        sell = _safe_num(row.get("15M Sell-side Liquidity"), np.nan)
        buy_dist = (buy - price) / price * 100 if np.isfinite(buy) else np.nan
        sell_dist = (price - sell) / price * 100 if np.isfinite(sell) else np.nan
        if str(row.get("15M Upside Sweep", "NO")).upper() == "YES":
            liq_state = "SWEEP UP"
        elif str(row.get("15M Downside Sweep", "NO")).upper() == "YES":
            liq_state = "SWEEP DOWN"
        elif np.isfinite(buy_dist) and 0 <= buy_dist <= LIQ_NEAR_PCT:
            liq_state = "NEAR BUY-SIDE LIQUIDITY"
        elif np.isfinite(sell_dist) and 0 <= sell_dist <= LIQ_NEAR_PCT:
            liq_state = "NEAR SELL-SIDE LIQUIDITY"
        else:
            liq_state = "NOT NEAR"

        # 11) Big Move V2 — directional votes + volatility/volume context
        d15 = str(row.get("15M", "WAIT")).upper()
        bull = [hn > 0, np.isfinite(obv_slope) and obv_slope >= 0.10, orb == "ORB UP",
                dist_high <= 0.30, side == "BUY", d15 == "BUY"]
        bear = [hn < 0, np.isfinite(obv_slope) and obv_slope <= -0.10, orb == "ORB DOWN",
                dist_low <= 0.30, side == "SELL", d15 == "SELL"]
        nb, ns = sum(bull), sum(bear)
        if nb >= 3 and nb >= ns + 2:
            direction, votes = "UP", nb
        elif ns >= 3 and ns >= nb + 2:
            direction, votes = "DOWN", ns
        else:
            direction, votes = "WAIT", max(nb, ns)
        score = (15 if squeeze else 0)
        score += 15 if (np.isfinite(atr_ratio) and atr_ratio >= 1.3) else 8 if (np.isfinite(atr_ratio) and atr_ratio >= 1.1) else 0
        score += 20 if rvol >= 2.0 else 10 if rvol >= 1.5 else 0
        score += int(round(votes * 7.5))
        score = int(min(100, score))
        if direction == "WAIT":
            score = min(score, 55)
        reason = (f"votes UP {nb}/6, DOWN {ns}/6; squeeze={'Y' if squeeze else 'N'}; "
                  f"ATR x{atr_ratio:.2f}; RVOL {rvol:.2f}x; MACD {macd_state}; {orb}")

        out.update({
            "5M MACD State": macd_state, "5M MACD Hist": round(hn, 4),
            "5M BB Squeeze": "SQUEEZE" if squeeze else "NORMAL",
            "5M BB Width Pctile": round(pctile, 1) if np.isfinite(pctile) else None,
            "5M ATR Expansion": round(atr_ratio, 2) if np.isfinite(atr_ratio) else None,
            "5M OBV Slope": round(obv_slope, 3) if np.isfinite(obv_slope) else None,
            "5M Volume Z": round(vz, 2), "Big Volume RVOL": round(rvol, 2),
            "5M Turnover Cr": round(turn_last, 3), "Session Turnover Cr": round(turn_sess, 2),
            "ORB State": orb, "Day High Dist %": round(dist_high, 3), "Day Low Dist %": round(dist_low, 3),
            "Gap %": round(gap, 2) if np.isfinite(gap) else None,
            "Big Volume Class": vclass, "Big Volume Side": side,
            "Liq Buy Dist %": round(buy_dist, 3) if np.isfinite(buy_dist) else None,
            "Liq Sell Dist %": round(sell_dist, 3) if np.isfinite(sell_dist) else None,
            "Liquidity State": liq_state,
            "Big Move V2 Direction": direction, "Big Move V2 Score": score,
            "Big Move V2 Reason": reason + ". Screening score, not probability.",
            "Formula Status": "OK",
        })
        return out
    except Exception as exc:
        out["Formula Status"] = "ERROR: " + str(exc)[:120]
        return out


# ---------------------------------------------------------------------
# FIXED CAPTURE: BUY / SELL / SWEEP signals (replaces _capture_signal_events)
# Old bug: any 15M breakout flag threw away even valid BUY/SELL signals,
# and pure breakouts could never be saved.
# ---------------------------------------------------------------------
def _capture_signal_events(result, reference_time=None):
    if not isinstance(result, pd.DataFrame) or result.empty:
        return 0
    now_text = datetime.now(IST).isoformat(timespec="seconds")
    actionable = {"BUY", "SELL", "PRE-BIG BUY", "PRE-BIG SELL"}
    rows = []
    for _, row in result.iterrows():
        if str(row.get("Data Quality", "PASS")).strip().upper() != "PASS":
            continue
        symbol = str(row.get("Symbol", "")).strip()
        if not symbol:
            continue
        final_signal = str(row.get("Final Signal", "WAIT") or "WAIT").strip().upper()
        breakout = str(row.get("Breakout Signal", "WAIT") or "WAIT").strip().upper()
        multi = str(row.get("Multi-TF Breakout", "WAIT") or "WAIT").strip().upper()
        advanced = str(row.get("Advanced Big Move", "") or "").strip().upper()
        if advanced == "FALSE BREAKOUT RISK":
            continue
        signal_type, direction = None, None
        if final_signal in actionable:
            direction = "UP" if "BUY" in final_signal else "DOWN"
            # Skip only when a confirmed OPPOSITE move exists.
            if (direction == "UP" and (breakout == "BREAKDOWN" or advanced == "SELL CONFIRMED")) or \
               (direction == "DOWN" and (breakout == "BREAKOUT" or advanced == "BUY CONFIRMED")):
                continue
            if breakout in ("BREAKOUT", "BREAKDOWN"):
                signal_type = f"{final_signal} / CONFIRMED {breakout}"
            elif advanced in ("BUY CONFIRMED", "SELL CONFIRMED"):
                signal_type = advanced
            else:
                signal_type = final_signal
        elif advanced in ("BUY CONFIRMED", "SELL CONFIRMED"):
            signal_type = advanced
            direction = "UP" if advanced == "BUY CONFIRMED" else "DOWN"
        elif breakout in ("BREAKOUT", "BREAKDOWN") and multi in ("BREAKOUT CONFIRMED", "BREAKDOWN CONFIRMED"):
            signal_type = f"CONFIRMED {breakout}"
            direction = "UP" if breakout == "BREAKOUT" else "DOWN"
        if not signal_type:
            continue
        candle_time = _as_ist_timestamp(row.get("Signal Time (IST)"))
        if candle_time is None or not _is_fresh_signal_time(candle_time, now=reference_time):
            continue
        price = _safe_num(row.get("Entry Price"), 0.0)
        if price <= 0:
            price = _safe_num(row.get("Last Price"), 0.0)
        if price <= 0:
            continue
        stop, target = _risk_levels_for_event(row, direction, price)
        yahoo_symbol = str(row.get("Yahoo Symbol", "") or "").strip()
        ctext = candle_time.isoformat(timespec="seconds")
        rows.append((f"{symbol}|{signal_type}|{ctext}", symbol, yahoo_symbol, signal_type, direction,
                     float(price), ctext, now_text, now_text, stop, target))
    if not rows:
        return 0
    with _memory_connect() as conn:
        rows = _drop_recent_duplicate_events(conn, rows)
        if not rows:
            return 0
        before = conn.total_changes
        conn.executemany("""INSERT OR IGNORE INTO signal_events
            (event_key,symbol,yahoo_symbol,signal_type,direction,signal_price,
             signal_time_ist,created_at_ist,current_price_time_ist,stop_price,target_price,
             risk_reward,data_status,status,outcome)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,1.5,'WAITING','ACTIVE','PENDING')""", rows)
        return conn.total_changes - before


# ---------------------------------------------------------------------
# FIXED CAPTURE: BIG VOLUME (replaces _capture_volume_expansion_events)
# Old bug: no freshness check, so yesterday's / restored candles showed as "live".
# ---------------------------------------------------------------------
def _capture_volume_expansion_events(result, threshold=BIG_VOL_RVOL_MIN, reference_time=None):
    if not isinstance(result, pd.DataFrame) or result.empty:
        return 0
    now_text = datetime.now(IST).isoformat(timespec="seconds")
    max_age = {"5M": 30, "15M": 60, "1H": 120}
    rows = []
    for _, row in result.iterrows():
        if str(row.get("Data Quality", "PASS")).strip().upper() != "PASS":
            continue
        symbol = str(row.get("Symbol", "") or "").strip()
        if not symbol:
            continue
        for tf in TIMEFRAMES:
            rvol = _safe_num(row.get(f"{tf} RVOL"), 0.0)
            direction = str(row.get(f"{tf} Candle Direction", "WAIT")).upper()
            volume = _safe_num(row.get(f"{tf} Candle Volume"), 0.0)
            average = _safe_num(row.get(f"{tf} Average Volume 20"), 0.0)
            price = _safe_num(row.get("Last Price"), 0.0)
            close_text = row.get(f"{tf} Candle Close (IST)")
            stamp = _as_ist_timestamp(close_text)
            if (rvol < threshold or direction not in ("BUY", "SELL") or volume <= 0
                    or average <= 0 or stamp is None or not _candle_is_recent(close_text, max_age[tf], now=reference_time)):
                continue
            stext = stamp.isoformat(timespec="seconds")
            rows.append((f"{symbol}|{tf}|{direction}|{stext}", symbol, tf, direction, volume, average,
                         rvol, price if price > 0 else None, stext, now_text))
    if not rows:
        return 0
    with _memory_connect() as conn:
        before = conn.total_changes
        conn.executemany("""INSERT OR IGNORE INTO volume_expansion_events
            (event_key,symbol,timeframe,direction,candle_volume,average_volume,rvol,last_price,
             signal_time_ist,captured_at_ist) VALUES (?,?,?,?,?,?,?,?,?,?)""", rows)
        return conn.total_changes - before


# ---------------------------------------------------------------------
# NEW PERSISTENT CACHES: big-movement stocks + liquidity watch
# ---------------------------------------------------------------------
_BIG_MOVE_STATUSES = {
    "PRE-BIG WATCH", "BREAKOUT DETECTED", "BREAKDOWN DETECTED", "RETEST WAITING",
    "BUY CONFIRMED", "SELL CONFIRMED", "RETEST HELD — 15M WAIT", "RETEST REJECTED — 15M WAIT",
}
_PREBIG_STATUSES = {
    "PRE-BIG BULLISH WATCH", "PRE-BIG BEARISH WATCH",
    "BULLISH BREAKOUT CONFIRMED", "BEARISH BREAKDOWN CONFIRMED",
}


def _capture_big_move_events(result, reference_time=None):
    """Cache big-movement stocks in SQLite (one row per symbol/source/status/day, updated in place)."""
    if not isinstance(result, pd.DataFrame) or result.empty:
        return 0
    _ensure_patch_tables()
    today, now_text = _today_text(), datetime.now(IST).isoformat(timespec="seconds")
    rows = []
    for _, row in result.iterrows():
        if str(row.get("Data Quality", "PASS")).strip().upper() != "PASS":
            continue
        symbol = str(row.get("Symbol", "") or "").strip()
        close_text = row.get("5M Candle Close (IST)")
        if not symbol or not _candle_is_recent(close_text, 30, now=reference_time):
            continue
        price = _safe_num(row.get("Last Price"), 0.0)
        cands = []
        adv = str(row.get("Advanced Big Move", "") or "").upper()
        if adv in _BIG_MOVE_STATUSES and _safe_num(row.get("Big Move Score"), 0) >= BIG_MOVE_MIN_SCORE:
            cands.append((adv, str(row.get("Big Move Direction", "WAIT")), _safe_num(row.get("Big Move Score")),
                          "ADVANCED", row.get("Big Move Range High"), row.get("Big Move Range Low"),
                          row.get("Big Move RVOL"), row.get("Big Move Reason")))
        pre = str(row.get("PRE-BIG Status", "") or "").upper()
        if pre in _PREBIG_STATUSES and _safe_num(row.get("PRE-BIG Score"), 0) >= BIG_MOVE_MIN_SCORE:
            cands.append((pre, str(row.get("PRE-BIG Direction", "WAIT")), _safe_num(row.get("PRE-BIG Score")),
                          "5M PRE-BIG", row.get("PRE-BIG Range High"), row.get("PRE-BIG Range Low"),
                          row.get("PRE-BIG RVOL"), row.get("PRE-BIG Reason")))
        v2_dir = str(row.get("Big Move V2 Direction", "WAIT") or "WAIT").upper()
        if v2_dir in ("UP", "DOWN") and _safe_num(row.get("Big Move V2 Score"), 0) >= 70:
            cands.append((f"BIG MOVE V2 {v2_dir}", v2_dir, _safe_num(row.get("Big Move V2 Score")),
                          "V2 FORMULA", None, None, row.get("Big Volume RVOL"), row.get("Big Move V2 Reason")))
        for status, direction, score, source, hi, lo, rvol, reason in cands:
            rows.append((f"{symbol}|{source}|{status}|{today}", symbol, status, direction, float(score),
                         price if price > 0 else None, _safe_num(hi, np.nan), _safe_num(lo, np.nan),
                         _safe_num(rvol, np.nan), source, str(reason or "")[:300],
                         now_text, now_text, str(close_text or "")))
    if not rows:
        return 0
    clean = [tuple(None if (isinstance(x, float) and not math.isfinite(x)) else x for x in r) for r in rows]
    with _memory_connect() as conn:
        conn.executemany("""INSERT INTO big_move_events
            (event_key,symbol,status,direction,score,price,range_high,range_low,rvol,source,reason,
             first_seen_ist,last_seen_ist,candle_time_ist) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(event_key) DO UPDATE SET score=excluded.score, price=excluded.price,
              range_high=excluded.range_high, range_low=excluded.range_low, rvol=excluded.rvol,
              reason=excluded.reason, last_seen_ist=excluded.last_seen_ist,
              candle_time_ist=excluded.candle_time_ist""", clean)
    return len(clean)


def _capture_liquidity_watch(result, reference_time=None):
    """Cache stocks that are near buy/sell-side liquidity or have a fresh sweep (today only)."""
    if not isinstance(result, pd.DataFrame) or result.empty:
        return 0
    _ensure_patch_tables()
    now_text = datetime.now(IST).isoformat(timespec="seconds")
    rows = []
    for _, row in result.iterrows():
        if str(row.get("Data Quality", "PASS")).strip().upper() != "PASS":
            continue
        symbol = str(row.get("Symbol", "") or "").strip()
        if not symbol or not _candle_is_recent(row.get("5M Candle Close (IST)"), 30, now=reference_time):
            continue
        price = _safe_num(row.get("Last Price"), 0.0)
        buy = _safe_num(row.get("15M Buy-side Liquidity"), np.nan)
        sell = _safe_num(row.get("15M Sell-side Liquidity"), np.nan)
        if price <= 0:
            continue
        buy_d = _safe_num(row.get("Liq Buy Dist %"), np.nan)
        sell_d = _safe_num(row.get("Liq Sell Dist %"), np.nan)
        if not np.isfinite(buy_d) and np.isfinite(buy):
            buy_d = (buy - price) / price * 100
        if not np.isfinite(sell_d) and np.isfinite(sell):
            sell_d = (price - sell) / price * 100
        state = bias = sweep_tf = sweep_time = None
        for tf in ("5M", "15M", "1H"):          # freshest confirmed sweep wins
            for flag, tcol, st_name, st_bias in (
                (f"{tf} Upside Sweep", f"{tf} Upside Sweep Time (IST)", "SWEEP UP", "SELL"),
                (f"{tf} Downside Sweep", f"{tf} Downside Sweep Time (IST)", "SWEEP DOWN", "BUY"),
            ):
                limit = {"5M": 30, "15M": 60, "1H": 120}[tf]
                if str(row.get(flag, "NO")).upper() == "YES" and _candle_is_recent(row.get(tcol), limit, now=reference_time):
                    cur = _as_ist_timestamp(sweep_time) if sweep_time else None
                    new = _as_ist_timestamp(row.get(tcol))
                    if cur is None or (new is not None and new > cur):
                        state, bias, sweep_tf, sweep_time = st_name, st_bias, tf, str(row.get(tcol))
        if state is None:
            if np.isfinite(buy_d) and 0 <= buy_d <= LIQ_NEAR_PCT:
                state, bias = "NEAR BUY-SIDE LIQUIDITY", "SELL-REJECT / BUY-BREAK WATCH"
            elif np.isfinite(sell_d) and 0 <= sell_d <= LIQ_NEAR_PCT:
                state, bias = "NEAR SELL-SIDE LIQUIDITY", "BUY-REJECT / SELL-BREAK WATCH"
            else:
                continue
        fin = lambda x: float(x) if np.isfinite(x) else None
        rows.append((symbol, price, fin(buy), fin(buy_d), fin(sell), fin(sell_d), state, bias,
                     sweep_tf, sweep_time, _safe_num(row.get("Big Volume RVOL"), 0.0), now_text))
    with _memory_connect() as conn:
        conn.execute("DELETE FROM liquidity_watch WHERE substr(updated_at_ist,1,10)<>?", (_today_text(),))
        if rows:
            conn.executemany("""INSERT OR REPLACE INTO liquidity_watch
                (symbol,price,buy_level,buy_dist_pct,sell_level,sell_dist_pct,state,bias,
                 sweep_tf,sweep_time_ist,rvol,updated_at_ist) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
    return len(rows)


def _capture_all_dashboard_events(result, reference_time=None):
    """One call for every dashboard ledger; freshness is measured at scan completion."""
    _ensure_patch_tables()
    if isinstance(result, pd.DataFrame) and "Data Quality" in result.columns:
        result = result[result["Data Quality"].astype(str).str.strip().str.upper() == "PASS"].copy()
    counts = {}
    for name, fn in (("signals", _capture_signal_events),
                     ("liquidity_sweeps", _capture_liquidity_sweep_events),
                     ("big_volume", _capture_volume_expansion_events),
                     ("big_move", _capture_big_move_events),
                     ("liquidity_watch", _capture_liquidity_watch)):
        try:
            counts[name] = fn(result, reference_time=reference_time)
        except Exception as exc:
            counts[name] = f"error: {str(exc)[:80]}"
    try:
        _prune_event_ledgers()
        cut = (pd.Timestamp.now(tz=IST) - pd.Timedelta(days=14)).isoformat(timespec="seconds")
        with _memory_connect() as conn:
            conn.execute("DELETE FROM big_move_events WHERE last_seen_ist<?", (cut,))
    except Exception:
        pass
    return counts


# ---------------------------------------------------------------------
# DASHBOARD BOARDS (replace the two old panel calls with this)
# ---------------------------------------------------------------------
def _hhmmss(series):
    return pd.to_datetime(series, errors="coerce", utc=True).dt.tz_convert(IST).dt.strftime("%H:%M:%S")


def _load_today_table(sql, params=()):
    try:
        with _memory_connect() as conn:
            return pd.read_sql_query(sql, conn, params=params)
    except Exception:
        return pd.DataFrame()


def _render_live_stock_boards():
    _ensure_patch_tables()
    today = _today_text()
    vol = _load_today_table("""SELECT symbol,timeframe,direction,candle_volume,average_volume,rvol,last_price,signal_time_ist
        FROM volume_expansion_events WHERE substr(signal_time_ist,1,10)=? ORDER BY signal_time_ist DESC""", (today,))
    liq = _load_today_table("""SELECT * FROM liquidity_watch WHERE substr(updated_at_ist,1,10)=?""", (today,))
    sweeps = _load_today_table("""SELECT symbol,timeframe,event_type,direction,rvol,signal_time_ist
        FROM liquidity_sweep_events WHERE event_type IN ('SWEEP UP','SWEEP DOWN')
        AND substr(signal_time_ist,1,10)=? ORDER BY signal_time_ist DESC""", (today,))
    big = _load_today_table("""SELECT symbol,status,direction,score,price,range_high,range_low,rvol,source,
        first_seen_ist,last_seen_ist,reason FROM big_move_events
        WHERE substr(last_seen_ist,1,10)=? ORDER BY score DESC, last_seen_ist DESC""", (today,))

    if not vol.empty:   # newest event per stock/timeframe
        vol = vol.drop_duplicates(["symbol", "timeframe"]).sort_values("rvol", ascending=False)

    st.markdown("### 🔥 LIVE STOCK BOARDS — BIG VOLUME • LIQUIDITY BUY/SELL/SWEEP • BIG MOVEMENT")
    m = st.columns(4)
    m[0].metric("Big Volume (≥2x) today", f"{vol['symbol'].nunique() if not vol.empty else 0}")
    m[1].metric("Liquidity / Sweep stocks", f"{len(liq)}")
    m[2].metric("Confirmed sweeps today", f"{len(sweeps)}")
    m[3].metric("Big-move stocks cached", f"{big['symbol'].nunique() if not big.empty else 0}")

    tab_v, tab_l, tab_b = st.tabs(["🔊 Big Volume", "💧 Liquidity BUY / SELL / SWEEP", "🚀 Big Movement"])
    with tab_v:
        if vol.empty:
            st.info("ఈ రోజు 2x volume stocks ఇంకా లేవు. Scan run చేయండి (market hours).")
        else:
            v = vol.copy()
            v["Time"] = _hhmmss(v["signal_time_ist"])
            v["Volume Signal"] = np.where(v["direction"].eq("BUY"), "BIG VOLUME BUY", "BIG VOLUME SELL")
            v = v.rename(columns={"symbol": "Stock", "timeframe": "TF", "direction": "Candle", "candle_volume": "Volume",
                                  "average_volume": "Avg Vol (20)", "rvol": "RVOL", "last_price": "LTP"})
            st.dataframe(v[["Stock", "TF", "LTP", "Candle", "Volume", "Avg Vol (20)", "RVOL", "Volume Signal", "Time"]].head(200),
                         hide_index=True, **_stretch())
            st.caption("RVOL = completed-candle volume ÷ average of previous 20 candles. Today's fresh candles only. Watch signal, not an entry.")
    with tab_l:
        st.markdown("**Confirmed SWEEP (fresh)**")
        s = liq[liq["state"].isin(["SWEEP UP", "SWEEP DOWN"])] if not liq.empty else liq
        if s.empty:
            st.caption("Fresh sweep ప్రస్తుతం లేదు.")
        else:
            s = s.copy(); s["Time"] = s["sweep_time_ist"].astype(str).str[-12:]
            s = s.rename(columns={"symbol": "Stock", "price": "LTP", "state": "Signal", "bias": "Bias", "sweep_tf": "TF", "rvol": "RVOL (5M)"})
            st.dataframe(s[["Stock", "TF", "Signal", "Bias", "LTP", "RVOL (5M)", "Time"]], hide_index=True, **_stretch())
        st.markdown("**Near BUY-side / SELL-side liquidity (within 0.5%)**")
        n = liq[liq["state"].str.startswith("NEAR")] if not liq.empty else liq
        if n.empty:
            st.caption("Liquidity దగ్గర stocks లేవు.")
        else:
            n = n.copy()
            n = n.rename(columns={"symbol": "Stock", "price": "LTP", "state": "State", "bias": "Watch",
                                  "buy_level": "Buy-side Level", "buy_dist_pct": "Dist to Buy-side %",
                                  "sell_level": "Sell-side Level", "sell_dist_pct": "Dist to Sell-side %"})
            st.dataframe(n[["Stock", "LTP", "State", "Watch", "Buy-side Level", "Dist to Buy-side %",
                            "Sell-side Level", "Dist to Sell-side %"]], hide_index=True, **_stretch())
        if not sweeps.empty:
            sw = sweeps.copy(); sw["Time"] = _hhmmss(sw["signal_time_ist"])
            with st.expander("All confirmed sweeps today (ledger)"):
                st.dataframe(sw.rename(columns={"symbol": "Stock", "timeframe": "TF", "event_type": "Signal",
                                                "direction": "Bias", "rvol": "RVOL"})[["Stock", "TF", "Signal", "Bias", "RVOL", "Time"]],
                             hide_index=True, **_stretch())
        st.caption("SWEEP UP = buy-side liquidity taken, price closed back below (SELL bias). SWEEP DOWN = sell-side liquidity taken, closed back above (BUY bias). Not an automatic entry.")
    with tab_b:
        if big.empty:
            st.info("ఈ రోజు big-movement stocks cache కాలేదు.")
        else:
            b = big.copy()
            b["First Seen"] = _hhmmss(b["first_seen_ist"]); b["Last Seen"] = _hhmmss(b["last_seen_ist"])
            b = b.rename(columns={"symbol": "Stock", "status": "Status", "direction": "Direction", "score": "Score",
                                  "price": "LTP", "range_high": "Range High", "range_low": "Range Low",
                                  "rvol": "RVOL", "source": "Source", "reason": "Reason"})
            st.dataframe(b[["Stock", "Status", "Direction", "Score", "LTP", "Range High", "Range Low", "RVOL",
                            "Source", "First Seen", "Last Seen", "Reason"]].head(200), hide_index=True, **_stretch())
        st.caption("Cached in SQLite, so stocks stay visible after browser refresh. Score = rule-based screening score, not probability.")


def show_ai_multi_agent_scanner(client=None):
    """Streamlit page. client is retained for app.py compatibility."""
    st.title("🚀 NSE AI PRO V18 — LIVE MARKET DASHBOARD + SCANNER")
    st.caption("Live Dashboard first • 17 OHLCV agents × 5M / 15M / 1H + supervised ML AI Brain + optional 1M / News")
    _render_live_dashboard()
    st.info("scanner.py supplies NSE/F&O symbols only (unchanged). Yahoo Finance supplies candles. FYERS is used only for 5-level Market Depth on the technical shortlist.")
    st.caption("Smart Memory: SQLite candles are loaded first; after the initial warm-up, the scanner refreshes a short 2-day overlap and merges it with saved candles. For speed, the extra 1M/20M scan is optional.")
    st.warning(
        "Yahoo intraday data may be delayed, missing or rate-limited. "
        "17-Agent Confirmation Strength is a heuristic score, not a probability. ML vote fractions are uncalibrated model estimates, not true probabilities or guaranteed win rates."
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
        batch_size = st.selectbox("Batch size", [10, 25, 50, 75, 100], index=2, key="ma_batch_size_v18", help="50 is the balanced default for full NSE scans. Use 10/25 if Yahoo rate-limits; use 75/100 only if your connection and Yahoo responses remain stable. Completed batches are checkpointed and can resume.")
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
        period = st.selectbox("Yahoo history", ["59d", "30d"], index=0, key="ma_period_v18")
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

    st.markdown("#### ☁️ Background Pre-Scan Memory")
    st.caption("Background worker saves NSE + F&O reports to Supabase. This button loads the latest completed cloud report into the existing V18 report pipeline.")
    if st.button("⚡ LOAD LATEST PRE-SCAN REPORT", key="ma_load_prescan", **_stretch()):
        try:
            with st.spinner("Loading latest saved NSE/F&O report..."):
                loaded_job_id, loaded_run, loaded_count = _load_latest_prescan_to_local_job(
                    universe, batch_size=batch_size, period=period, depth_limit=depth_limit
                )
            st.session_state["ma_scan_job_id"] = loaded_job_id
            st.session_state.pop("ma_scan_finalized_job", None)
            st.session_state["ma_restored_from_db"] = False
            st.session_state["ma_discarded"] = False
            st.success(
                f"Loaded {loaded_count:,} stocks • {loaded_run.get('scheduled_type', 'BACKGROUND')} • "
                f"completed {loaded_run.get('completed_at', 'time unavailable')}. Preparing V18 reports..."
            )
            st.rerun()
        except Exception as prescan_exc:
            st.error(f"Could not load background report: {prescan_exc}")
            st.info("Check Supabase credentials, SQL tables, and whether the scheduled GitHub Action completed successfully.")

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
    resume_col, cancel_col = st.columns(2)
    with resume_col:
        resume_scan_now = st.button("▶️ RESUME / CONTINUE SCAN", key="ma_resume_scan", **_stretch())
    with cancel_col:
        cancel_scan_now = st.button("🛑 CANCEL / DISCARD SCAN", key="ma_cancel_scan", **_stretch())

    # Expire yesterday's or stalled jobs before checking the session-bound job ID.
    _expire_stale_scan_jobs()
    # A cancel is durable in SQLite. Existing candle memory is intentionally preserved.
    job_id = st.session_state.get("ma_scan_job_id")
    if cancel_scan_now:
        st.session_state["ma_discarded"] = True
        st.session_state.pop("ma_restored_from_db", None)
        if job_id:
            with _memory_connect() as conn:
                conn.execute(
                    "UPDATE scan_jobs SET status='CANCELLED',updated_at=?,error=? WHERE job_id=? AND status IN ('RUNNING','PAUSED','DONE','FINALIZED')",
                    (datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S %Z"), "Discarded by user", job_id),
                )
        st.session_state.pop("ma_scan_job_id", None)
        st.session_state.pop("ma_scan_finalized_job", None)
        st.session_state.pop("multi_agent_results", None)
        st.rerun()

    # Restore the newest finalized scan after a browser refresh. Move it to DONE
    # so the existing report-building path reloads the saved rows from SQLite.
    job_id = st.session_state.get("ma_scan_job_id")
    job = _scan_job_get(job_id) if job_id else None
    if ("multi_agent_results" not in st.session_state and not start_scan_requested and not resume_scan_now
            and not st.session_state.get("ma_discarded", False)
            and not (job and job["status"] in ("RUNNING", "PAUSED", "DONE"))):
        with _memory_connect() as conn:
            latest_job = conn.execute(
                "SELECT job_id,status FROM scan_jobs ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            # sqlite3.Row is not configured in _memory_connect(); fetchone() is a tuple.
            finalized = latest_job if latest_job and latest_job[1] == "FINALIZED" else None
            if finalized:
                conn.execute("UPDATE scan_jobs SET status='DONE' WHERE job_id=?", (finalized[0],))
        if finalized:
            st.session_state["ma_scan_job_id"] = finalized[0]
            st.session_state["ma_restored_from_db"] = True
            st.session_state.pop("ma_scan_finalized_job", None)
            st.rerun()

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
            st.session_state["ma_discarded"] = False
            st.session_state.pop("ma_restored_from_db", None)
            st.session_state.pop("ma_ai_export_signature", None)
            st.session_state.pop("ma_ai_apply_sig", None)
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
        if result.empty:
            st.error("No completed stock rows were saved for this scan job. The job has been paused for a safe retry; check Yahoo data and press RESUME.")
            try:
                with _memory_connect() as conn:
                    conn.execute(
                        "UPDATE scan_jobs SET status='PAUSED',updated_at=?,error=? WHERE job_id=? AND status='DONE'",
                        (datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S %Z"), "Restore returned zero rows; paused instead of repeating a DONE restore.", job_id),
                    )
            except Exception:
                pass
            st.session_state.pop("ma_restored_from_db", None)
            return
        restored_from_db = bool(st.session_state.pop("ma_restored_from_db", False))

        # A browser refresh restores technical rows from SQLite. Do not repeat
        # optional external calls (Yahoo news / FYERS depth) during that restore.
        if restored_from_db:
            result["News Agent"] = "NOT FETCHED ON RESTORE" if news_enabled else "DISABLED"
            result["News Score"] = 0
            result["News Reason"] = (
                "Restored from SQLite; external news call intentionally skipped"
                if news_enabled else "News disabled"
            )

            depth_candidates = result[result["Final Signal"].isin(
                ["BUY", "SELL", "PRE-BIG BUY", "PRE-BIG SELL"]
            )].head(depth_limit)
            depth_columns = [
                "FYERS Symbol", "Depth Status", "Depth Signal", "Best Bid", "Best Ask",
                "Spread", "Buy Qty (5L)", "Sell Qty (5L)", "Depth Imbalance %",
                "Depth Time (IST)", "Depth Message",
            ]
            for col in depth_columns:
                result[col] = None
            result["Depth Status"] = "NOT FETCHED ON RESTORE"
            result["Depth Message"] = "Restored from SQLite; FYERS depth call intentionally skipped"
            depth_table = pd.DataFrame(columns=["Symbol"] + depth_columns)
        else:
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
            if not restored_from_db:
                _save_scan_history(result, stamp.strftime("%Y-%m-%d %H:%M:%S %Z"))
        except Exception as memory_exc:
            st.warning(f"SQLite scan history save issue: {memory_exc}")
        csv_bytes = result.to_csv(index=False).encode("utf-8-sig")

        st.session_state["multi_agent_results"] = result
        st.session_state["multi_agent_csv"] = csv_bytes
        # The Excel workbook is built once, in the AI block below, after the AI
        # columns are applied. Clearing the signatures forces that single build.
        st.session_state["multi_agent_xlsx"] = None
        st.session_state["multi_agent_xlsx_error"] = ""
        st.session_state.pop("ma_ai_export_signature", None)
        st.session_state.pop("ma_ai_apply_sig", None)
        st.session_state["multi_agent_scan_info"] = {
            "universe": universe, "source_symbols": len(symbols),
            "unique_tickers": len(unique_items), "batches": len(batches),
            "completed_at": stamp.strftime("%Y-%m-%d %H:%M:%S IST"),
            "file_stamp": stamp.strftime("%Y%m%d_%H%M%S"),
        }
        if restored_from_db:
            status.success(
                f"Restored {len(result)} Yahoo rows from SQLite. News and FYERS depth were not re-fetched."
            )
        else:
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

    # Re-apply the hard quality gate to restored/session-cached rows as well.
    if "Data Quality" in result.columns:
        failed_quality = result["Data Quality"].astype(str).str.strip().str.upper() != "PASS"
        if failed_quality.any():
            for col, value in (
                ("Advanced Big Move", "NO DATA"),
                ("Action Signal", "NO DATA"),
                ("Early Signal", "WAIT — DATA QUALITY FAIL"),
                ("Early Direction", "WAIT"),
                ("Early Confirmation", "NOT CONFIRMED"),
            ):
                result.loc[failed_quality, col] = value
            if "Early Signal Reason" in result.columns and "Data Quality Reason" in result.columns:
                result.loc[failed_quality, "Early Signal Reason"] = result.loc[failed_quality, "Data Quality Reason"].astype(str)
            st.session_state["multi_agent_results"] = result

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
    early_decision_defaults = {
        "Early Signal": "NOT CALCULATED", "Early Direction": "WAIT",
        "Early Evidence Score": 0, "Early Confirmation": "NOT CONFIRMED",
        "Early Signal Reason": "Run a fresh scan to calculate the additive early-decision layer",
        "Early Signal Time (IST)": None,
    }
    for col_name, default_value in early_decision_defaults.items():
        if col_name not in result.columns:
            result[col_name] = default_value
    # Recalculate only the new fields for restored/older session rows; legacy columns are preserved.
    if "Early Signal" in result.columns and (result["Early Signal"] == "NOT CALCULATED").any():
        new_early = result.apply(_early_signal_decision, axis=1, result_type="expand")
        for col_name in early_decision_defaults:
            result[col_name] = new_early[col_name]
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
        "Buy-side Liquidity Time (IST)": None, "Sell-side Liquidity Time (IST)": None,
        "Upside Sweep": "NO DATA", "Downside Sweep": "NO DATA",
        "Upside Sweep Time (IST)": None, "Downside Sweep Time (IST)": None,
        "Equal High Liquidity": "NO DATA", "Equal Low Liquidity": "NO DATA",
        "Liquidity Sweep Detail": "Run a fresh scan for liquidity levels",
        "Candle Volume": None, "Average Volume 20": None, "RVOL": None,
        "Volume Signal": "NO DATA", "Candle Direction": "WAIT",
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

    # SIMPLE MAIN REPORT: compact screen view; the full result DataFrame remains untouched.
    st.subheader("📊 SIMPLE SCAN REPORT — QUICK VIEW")
    signal_counts = result.get("Final Signal", pd.Series("WAIT", index=result.index)).fillna("WAIT").value_counts()
    quick_metrics = st.columns(5)
    for metric_col, metric_name in zip(quick_metrics, ("BUY", "SELL", "PRE-BIG BUY", "PRE-BIG SELL", "WAIT")):
        metric_col.metric(metric_name, int(signal_counts.get(metric_name, 0)))
    quick_filter = st.selectbox(
        "Quick filter", ["ALL", "BUY", "SELL", "PRE-BIG BUY", "PRE-BIG SELL", "WAIT", "NO DATA"],
        key="ma_simple_report_filter_v1",
    )
    quick_view = result.copy()
    if quick_filter != "ALL":
        quick_view = quick_view[quick_view["Final Signal"].astype(str) == quick_filter]
    quick_sort_cols = [c for c in ("Best Stock Score", "Confirmation Strength %") if c in quick_view.columns]
    if quick_sort_cols:
        quick_view = quick_view.sort_values(quick_sort_cols, ascending=[False] * len(quick_sort_cols))
    quick_cols = [c for c in [
        "Symbol", "Final Signal", "Best Stock Score", "Last Price", "Entry Price",
        "Stop Loss", "5M", "15M", "1H", "Signal Time (IST)", "Best Stock Reason",
    ] if c in quick_view.columns]
    st.caption("BUY/SELL = confirmed technical signal • PRE-BIG = early watch, not confirmed • WAIT = no clear setup. Full analysis is below.")
    st.dataframe(quick_view[quick_cols].head(300), hide_index=True, **_stretch())

    with st.expander("🔎 ADVANCED REPORTS — LIQUIDITY, SWEEPS, AI, BIG MOVE & AGENT DETAILS", expanded=False):
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
            & (result["Pre-Move 15M Alignment"] != "NOT ALIGNED")
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

        with st.container():
            st.subheader("Data Quality + Market Regime checks")
            st.write(
                "Data Quality checks the 5M/15M/1H OHLCV frames, minimum candle count, "
                "invalid prices/volume, duplicate timestamps and intraday staleness. "
                "Market Regime is descriptive context only; it does not force BUY/SELL."
            )
            quality_cols = ["Symbol", "Data Quality", "Data Quality Reason", "Market Regime", "Regime Strength", "Regime ADX", "Regime ATR %", "Regime BB Width %"]
            st.dataframe(result[quality_cols].head(100), hide_index=True, **_stretch())

        # Restore the previously trained model once per Streamlit session.
        if not st.session_state.get("ma_real_ai_model"):
            _persisted = _load_persisted_ai_brain()
            if _persisted:
                st.session_state["ma_real_ai_model"] = _persisted

        # Real AI Brain training uses saved, completed 5M candles and chronological holdout.
        st.subheader("🧠 REAL AI BRAIN — Hybrid ML")
        ai_col1, ai_col2 = st.columns(2)
        with ai_col1:
            train_ai_now = st.button("🧠 TRAIN / REFRESH REAL AI BRAIN", key="ma_train_real_ai")
        with ai_col2:
            ai_bundle = st.session_state.get("ma_real_ai_model")
            if ai_bundle:
                wf_score = ai_bundle["metrics"].get("walk_forward_balanced_accuracy")
                st.metric("AI Walk-Forward Balanced Accuracy", f"{wf_score*100:.1f}%" if wf_score is not None else "NOT AVAILABLE")
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
                    f"walk-forward balanced accuracy "
                    f"{trained_bundle['metrics']['walk_forward_balanced_accuracy']*100:.1f}% "
                    f"({trained_bundle['metrics']['walk_forward_folds']} folds) • trained {trained_bundle['metrics']['trained_at']}"
                    if trained_bundle['metrics']['walk_forward_balanced_accuracy'] is not None else
                    f"walk-forward validation unavailable; holdout balanced accuracy {trained_bundle['metrics']['balanced_accuracy']*100:.1f}% • trained {trained_bundle['metrics']['trained_at']}"
                )
            else:
                st.session_state["ma_real_ai_train_error"] = train_error
                st.error(f"AI retraining failed; the previous model is kept. Reason: {train_error}")
        ai_bundle = st.session_state.get("ma_real_ai_model")
        if ai_bundle:
            wf_value = ai_bundle["metrics"].get("walk_forward_balanced_accuracy")
            wf_text = f"walk-forward balanced accuracy {wf_value*100:.1f}%" if wf_value is not None else "walk-forward balanced accuracy NOT AVAILABLE"
            st.caption(
                f"Chronological 80/20 holdout + {ai_bundle['metrics'].get('walk_forward_folds', 0)} expanding walk-forward folds • "
                f"{ai_bundle['metrics']['train_samples']:,} train / {ai_bundle['metrics']['test_samples']:,} holdout • "
                f"holdout balanced accuracy {ai_bundle['metrics']['balanced_accuracy']*100:.1f}% • {wf_text}. "
                "Targets: next 3 completed 5M candles within the same session; deadband is max(0.10%, 0.8× ATR% × √3). RF vote fractions are uncalibrated model estimates, not probabilities, true odds or a guaranteed win rate."
            )
            ai_apply_sig = (st.session_state.get("ma_scan_job_id"), ai_bundle.get("metrics", {}).get("trained_at"), len(result))
            if st.session_state.get("ma_ai_apply_sig") != ai_apply_sig:
                result = _apply_real_ai_brain(result)
                st.session_state["multi_agent_results"] = result
                st.session_state["multi_agent_csv"] = result.to_csv(index=False).encode("utf-8-sig")
                st.session_state["ma_ai_apply_sig"] = ai_apply_sig
            else:
                result = st.session_state.get("multi_agent_results", result)
            ai_cols = ["Symbol", "AI Direction", "AI Up Model Estimate %", "AI Down Model Estimate %",
                       "AI Sideways Model Estimate %", "AI Brain Signal", "Final Signal"]
            st.dataframe(result[ai_cols].head(30), hide_index=True, **_stretch())
        else:
            # Ensure the report schema is stable even before 500 labelled samples exist.
            ai_apply_sig = (st.session_state.get("ma_scan_job_id"), "none", len(result))
            if st.session_state.get("ma_ai_apply_sig") != ai_apply_sig:
                result = _apply_real_ai_brain(result)
                st.session_state["multi_agent_results"] = result
                st.session_state["multi_agent_csv"] = result.to_csv(index=False).encode("utf-8-sig")
                st.session_state["ma_ai_apply_sig"] = ai_apply_sig
            else:
                result = st.session_state.get("multi_agent_results", result)
            st.warning(
                "Real AI Brain is not active: " +
                str(st.session_state.get("ma_real_ai_train_error") or "training data not available") +
                " Collect more completed 5M candle history and press TRAIN / REFRESH."
            )

        # Idempotent event capture: repeated Streamlit reruns do not create duplicate events.
        try:
            _capture_job = _scan_job_get(st.session_state.get("ma_scan_job_id"))
            _capture_reference = (
                _as_ist_timestamp(_capture_job.get("updated_at"))
                if _capture_job else pd.Timestamp.now(tz=IST)
            )
            _cap = _capture_all_dashboard_events(result, reference_time=_capture_reference)
            _n = lambda k: _cap.get(k) if isinstance(_cap.get(k), int) else 0
            new_events, new_liquidity_events, new_volume_events = _n('signals'), _n('liquidity_sweeps'), _n('big_volume')
            if new_events:
                st.caption(f"Live Dashboard: {new_events} new signal event(s) saved to SQLite.")
            if new_liquidity_events:
                st.caption(f"Live Dashboard: {new_liquidity_events} new liquidity/sweep event(s) saved to SQLite.")
            if new_volume_events:
                st.caption(f"Live Dashboard: {new_volume_events} new >=2x volume event(s) saved to SQLite.")
        except Exception as event_exc:
            st.warning(f"Live signal event save issue: {event_exc}")

        # Additive early signal report; does not alter existing BUY/SELL or Action Signal fields.
        st.subheader("⚡ EARLY SIGNAL DECISION — BEFORE MOVE + FALSE-SIGNAL FILTER")
        early_decision_cols = [c for c in [
            "Symbol", "Last Price", "Early Signal", "Early Direction", "Early Evidence Score",
            "Early Confirmation", "Early Signal Time (IST)", "20M Status", "20M Direction",
            "20M Pre-Move Score", "Directional Pre-Move", "Pre-Move Direction",
            "Pre-Move 15M Alignment", "5M", "15M", "1H", "Market Regime", "Early Signal Reason",
        ] if c in result.columns]
        early_decision_view = result[result["Early Signal"].astype(str).str.startswith((
            "EARLY WATCH", "BREAKOUT CONFIRMED", "WAIT — 1M/5M CONFLICT",
            "WAIT — 15M NOT ALIGNED", "WAIT — 1H CONFLICT"
        ))].copy()
        if not early_decision_view.empty:
            early_decision_view = early_decision_view.sort_values(
                ["Early Evidence Score", "20M Pre-Move Score"], ascending=[False, False]
            )
            st.dataframe(early_decision_view[early_decision_cols].head(200), hide_index=True, height=360, **_stretch())
        else:
            st.info("ప్రస్తుతం 1M/5M ముందస్తు setup + 15M దిశ సరిపోలిన signal లేదు. WAIT అనేది కూడా valid result.")
        st.caption("Early Evidence Score is a screening score, not a probability. 1M/5M signals are WATCH only until confirmation; breakout needs retest/continuation checks. Legacy Final Signal and Action Signal remain unchanged.")

        # Liquidity & sweep event-time panel (15M reference; timestamps are completed-candle close times).
        st.subheader("💧 BUY / SELL LIQUIDITY + SWEEP SIGNAL TIME (IST)")
        liquidity_time_cols = [
            "Symbol", "Last Price", "Buy-side Liquidity", "Buy-side Liquidity Time (IST)",
            "Sell-side Liquidity", "Sell-side Liquidity Time (IST)",
            "Upside Sweep", "Upside Sweep Time (IST)",
            "Downside Sweep", "Downside Sweep Time (IST)", "Liquidity Sweep Detail",
            "15M Buy-side Liquidity", "15M Buy-side Liquidity Time (IST)",
            "15M Sell-side Liquidity", "15M Sell-side Liquidity Time (IST)",
            "15M Upside Sweep", "15M Upside Sweep Time (IST)",
            "15M Downside Sweep", "15M Downside Sweep Time (IST)",
        ]
        liquidity_time_cols = [c for c in liquidity_time_cols if c in result.columns]
        liquidity_event_mask = (
            result.get("15M Upside Sweep", pd.Series("NO", index=result.index)).eq("YES")
            | result.get("15M Downside Sweep", pd.Series("NO", index=result.index)).eq("YES")
        )
        liquidity_panel = result.loc[liquidity_event_mask, liquidity_time_cols].copy()
        if not liquidity_panel.empty:
            st.dataframe(liquidity_panel, hide_index=True, height=300, **_stretch())
        else:
            st.info("ప్రస్తుతం 15M completed candleలో confirmed Sweep Up / Sweep Down signal లేదు. Liquidity levels మరియు వాటి formation times క్రింది main reportలో ఉంటాయి.")
        st.caption("Liquidity Time = ఆ liquidity level ఏర్పడిన pivot candle close time. Sweep Time = sweep confirm అయిన completed candle close time. అన్ని timestamps IST.")

        st.subheader("🚀 V18 ADVANCED BIG MOVEMENT — FALSE BREAKOUT + CONTINUATION")
        advanced_cols = [c for c in [
            "Symbol", "Last Price", "Advanced Big Move", "Big Move Direction", "Big Move Score",
            "False Breakout Risk", "Breakout Retest Status", "5M Retest Check", "15M Trend Check", "Momentum Continuation",
            "Big Move Range High", "Big Move Range Low", "Big Move RVOL", "Big Move Reason",
        ] if c in result.columns]
        advanced_view = result[result.get("Advanced Big Move", pd.Series(index=result.index, dtype=str)).isin([
            "PRE-BIG WATCH", "BREAKOUT DETECTED", "BREAKDOWN DETECTED", "RETEST WAITING",
            "BUY CONFIRMED", "SELL CONFIRMED", "RETEST HELD — 15M WAIT",
            "RETEST REJECTED — 15M WAIT", "FALSE BREAKOUT RISK", "WAIT — SESSION WARM-UP",
        ])].copy()
        if not advanced_view.empty:
            advanced_view = advanced_view.sort_values("Big Move Score", ascending=False)
            st.dataframe(advanced_view[advanced_cols], hide_index=True, height=420, **_stretch())
        else:
            st.info("No advanced big-movement setup currently meets the screening conditions.")
        st.caption("Big Move Score is a rule-based screening score, not a probability or guaranteed return. Retest and continuation require subsequent completed candles; validate using historical trade backtesting.")

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
            "Buy-side Liquidity", "Buy-side Liquidity Time (IST)", "Sell-side Liquidity", "Sell-side Liquidity Time (IST)", "Upside Sweep", "Upside Sweep Time (IST)", "Downside Sweep", "Downside Sweep Time (IST)", "Liquidity Sweep Detail",
            "5M Buy-side Liquidity", "5M Buy-side Liquidity Time (IST)", "5M Sell-side Liquidity", "5M Sell-side Liquidity Time (IST)", "5M Upside Sweep", "5M Upside Sweep Time (IST)", "5M Downside Sweep", "5M Downside Sweep Time (IST)",
            "15M Buy-side Liquidity", "15M Buy-side Liquidity Time (IST)", "15M Sell-side Liquidity", "15M Sell-side Liquidity Time (IST)", "15M Upside Sweep", "15M Upside Sweep Time (IST)", "15M Downside Sweep", "15M Downside Sweep Time (IST)",
            "1H Buy-side Liquidity", "1H Buy-side Liquidity Time (IST)", "1H Sell-side Liquidity", "1H Sell-side Liquidity Time (IST)", "1H Upside Sweep", "1H Upside Sweep Time (IST)", "1H Downside Sweep", "1H Downside Sweep Time (IST)",
            "AI Direction", "AI Up Model Estimate %", "AI Down Model Estimate %",
            "AI Sideways Model Estimate %", "AI Brain Signal", "AI Model Status",
            "Advanced Big Move", "Big Move Direction", "Big Move Score", "False Breakout Risk",
            "Breakout Retest Status", "5M Retest Check", "15M Trend Check", "Momentum Continuation", "Big Move Range High",
            "Big Move Range Low", "Big Move RVOL", "Big Move Reason",
            "18th Agent", "Best Stock Score", "Best Stock Side", "Best Stock Grade",
            "Best Stock Filter", "Best Stock Reason",
            "Early Signal", "Early Direction", "Early Evidence Score", "Early Confirmation",
            "Early Signal Time (IST)", "1M Candle Close (IST)", "Early Signal Reason",
            "20M Status", "20M Direction", "20M Pre-Move Score", "20M Range High", "20M Range Low",
            "20M Range %", "20M RVOL", "20M Pressure %", "20M Last Close", "20M Reason",
            "5M", "15M", "1H", "5M Score", "15M Score", "1H Score",
            "5M Candles", "15M Candles", "1H Candles", "Last Price", "15M RSI",
            "News Agent", "News Score", "News Reason", "Depth Status", "Depth Signal",
            "Best Bid", "Best Ask", "Spread", "Buy Qty (5L)", "Sell Qty (5L)",
            "Depth Imbalance %", "Depth Time (IST)", "Errors",
        ]
        st.subheader("🚀 EARLY BIG MOVE CATCHER — 5M Compression + Volume Build-up")
        st.caption(f"{int(result.get('Symbol', pd.Series(dtype=str)).nunique()):,} unique scanned symbols: 13 completed 5M candles today (12-bar base + 1 completed trigger candle), rising volume and directional bias; breakout is confirmed only after a completed candle closes beyond the range with strong body and RVOL.")
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
        st.caption(f"Showing top 300 of {len(view):,} rows in the main table. Full results remain available in CSV/Excel.")
        st.dataframe(view[summary_cols].head(300), hide_index=True, **_stretch())

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
    if st.button("📊 Prepare Excel (takes time)", key="ma_prep_xlsx"):
        with st.spinner("Building full Excel workbook..."):
            xlsx_bytes, xlsx_error = _build_excel(result, datetime.now(IST))
        st.session_state["multi_agent_xlsx"] = xlsx_bytes
        st.session_state["multi_agent_xlsx_error"] = xlsx_error
        if xlsx_bytes:
            st.success("Excel workbook is ready. Use the Download Full Excel button below.")

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
