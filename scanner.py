"""
scanner.py
══════════════════════════════════════════════════════════════════════════
FYERS API V3 + Streamlit — 15-Minute Order Block (Smart Money Concepts)
Scanner & Live Dashboard.

WHAT THIS FILE DOES
--------------------
- Scans a universe of NSE equity symbols using ONLY 15-minute candles
  pulled from the FYERS API V3 `history` endpoint.
- Detects fresh Bullish and Bearish Order Blocks using a strict rule set
  (see `detect_order_block_signal` docstring for the exact conditions).
- De-duplicates signals so the same confirming candle never re-fires the
  same signal twice, even across dashboard refreshes/app restarts (the
  dedup state is persisted to disk in `data/dedup_state.json`).
- Persists every fresh signal as:
    * a human-readable .txt file under signals/bullish/ or signals/bearish/
    * a row appended to a CSV history file
    * a row appended to a JSON Lines history file
    * a line in the rotating log file under logs/
- Renders a Plotly candlestick chart per signal (Order Block zone, Entry,
  Stop Loss, Target lines, and a volume subplot) and saves it as an image
  (or HTML fallback if a static-image engine like `kaleido` isn't
  installed) under charts/.
- Exposes `run_dashboard(fyers, symbols=None)` — a single Streamlit entry
  point that app.py can import and call directly, e.g.:

        import scanner
        scanner.run_dashboard(fyers)

INTEGRATION WITH app.py
------------------------
`app.py` is expected to own FYERS authentication/session creation and
pass the resulting `fyers` client object (an instance of
`fyers_apiv3.fyersModel.FyersModel`) into `run_dashboard(fyers)`. This
file never manages login/token flow itself — it only calls
`fyers.history(params)`.

Every FYERS `history` response follows the documented v3 shape:
    {"s": "ok", "candles": [[epoch, open, high, low, close, volume], ...]}
"""

from __future__ import annotations

import csv
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import streamlit as st

try:
    import plotly.graph_objects as go
    PLOTLY_AVAILABLE = True
except ImportError:  # pragma: no cover — dashboard degrades gracefully
    PLOTLY_AVAILABLE = False

try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
except Exception:  # pragma: no cover — safety net on very old Pythons
    from datetime import timezone
    IST = timezone(timedelta(hours=5, minutes=30))


# ══════════════════════════════════════════════════════════════════════════
# ── 1. DIRECTORY LAYOUT ──────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════

BASE_DIR = Path(__file__).resolve().parent

SIGNALS_DIR = BASE_DIR / "signals"
SIGNALS_BULLISH_DIR = SIGNALS_DIR / "bullish"
SIGNALS_BEARISH_DIR = SIGNALS_DIR / "bearish"
LOGS_DIR = BASE_DIR / "logs"
CHARTS_DIR = BASE_DIR / "charts"
DATA_DIR = BASE_DIR / "data"

CSV_HISTORY_PATH = DATA_DIR / "signal_history.csv"
JSONL_HISTORY_PATH = DATA_DIR / "signal_history.jsonl"
DEDUP_STATE_PATH = DATA_DIR / "dedup_state.json"
LOG_FILE_PATH = LOGS_DIR / "scanner.log"


def ensure_directories() -> None:
    """Creates every folder this module writes to, if missing. Safe to call
    on every run — idempotent and side-effect-free when folders exist."""
    for directory in (
        SIGNALS_DIR, SIGNALS_BULLISH_DIR, SIGNALS_BEARISH_DIR,
        LOGS_DIR, CHARTS_DIR, DATA_DIR,
    ):
        directory.mkdir(parents=True, exist_ok=True)


# ══════════════════════════════════════════════════════════════════════════
# ── 2. LOGGING ────────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════

def setup_logging() -> logging.Logger:
    """Configures a module-level logger that writes to logs/scanner.log
    (rotating manually is unnecessary here — file is small/append-only text
    lines) as well as stdout, for visibility when run under Streamlit."""
    ensure_directories()
    logger = logging.getLogger("ob_scanner")
    logger.setLevel(logging.INFO)

    if not logger.handlers:  # avoid duplicate handlers on Streamlit re-runs
        fmt = logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        file_handler = logging.FileHandler(LOG_FILE_PATH, encoding="utf-8")
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)

        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(fmt)
        logger.addHandler(stream_handler)

    return logger


logger = setup_logging()


# ══════════════════════════════════════════════════════════════════════════
# ── 3. CONFIGURATION ──────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════

# This scanner is intraday-only and ALWAYS uses 15-minute candles.
RESOLUTION = "15"
RESOLUTION_MINUTES = 15
TIMEFRAME_LABEL = "15 Minutes"

# How many days of 15-min history to request. FYERS caps intraday history
# lookback per-request; a handful of trading days is more than enough for
# swing-high/low + order-block detection.
HISTORY_LOOKBACK_DAYS = 5

# Swing high/low lookback window (in candles) used to define the
# "previous swing high/low" a breakout candle must clear.
SWING_LOOKBACK_CANDLES = 10

# Order-block search window: how many recent candles we scan backwards
# from the breakout candle to find the last opposite-colour candle.
ORDER_BLOCK_SEARCH_WINDOW = 15

# Volume confirmation: breakout candle volume must exceed this multiple of
# the 20-candle average volume.
VOLUME_AVG_WINDOW = 20
VOLUME_CONFIRMATION_MULTIPLIER = 1.0

# Risk/Reward multiples (in units of the Order Block zone height) used to
# derive Target 1 / Target 2 from Entry/Stop Loss.
TARGET1_RR_MULTIPLE = 1.5
TARGET2_RR_MULTIPLE = 2.5

# Network / rate-limit friendly scanning.
MAX_WORKERS = 8
BATCH_SIZE = 40
BATCH_PAUSE_SECONDS = 1.0

_HISTORY_MAX_RETRIES = 3
_HISTORY_BASE_DELAY_SECONDS = 1.0

# Default dashboard auto-refresh interval, in seconds.
DEFAULT_REFRESH_SECONDS = 30

# Fyers' public NSE Capital Market symbol master — used only as a
# convenience default universe when app.py doesn't supply its own symbol
# list to run_dashboard().
FYERS_NSE_CM_SYMBOL_MASTER = "https://public.fyers.in/sym_details/NSE_CM.csv"


# ══════════════════════════════════════════════════════════════════════════
# ── 4. TIME HELPERS (IST) ────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════

def now_ist() -> datetime:
    """Current time in IST (Asia/Kolkata) — single source of truth for
    'what time is it right now', as opposed to a candle's own timestamp."""
    return datetime.now(IST)


def to_ist(ts: pd.Timestamp) -> pd.Timestamp:
    """Normalizes a (possibly naive) pandas Timestamp to IST."""
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.tz_convert(IST)


def is_candle_closed(candle_open_time: pd.Timestamp, resolution_minutes: int = RESOLUTION_MINUTES) -> bool:
    """True only if the candle starting at `candle_open_time` has fully
    closed as of this exact moment. Guarantees signals are only ever
    generated off a completed 15-minute candle, never a still-forming one."""
    candle_close = to_ist(candle_open_time) + timedelta(minutes=resolution_minutes)
    return now_ist() >= candle_close


def format_ist(ts: pd.Timestamp) -> Tuple[str, str]:
    """Formats a candle Timestamp as ('DD-MMM-YYYY', 'HH:MM:SS IST'), in
    IST, exactly as it stands (15-minute candles already carry their true
    close-relevant open time, e.g. 09:15, 09:30, 09:45...)."""
    ts_ist = to_ist(ts)
    return ts_ist.strftime("%d-%b-%Y"), ts_ist.strftime("%H:%M:%S") + " IST"


# ══════════════════════════════════════════════════════════════════════════
# ── 5. DATA MODEL ─────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class OrderBlockSignal:
    """A single, fully-formed, fresh Order Block signal ready for display,
    persistence, alerting, and charting."""
    symbol: str
    direction: str                  # "BUY" or "SELL"
    signal_date: str                # DD-MMM-YYYY (IST)
    signal_time: str                # HH:MM:SS IST
    timeframe: str                  # "15 Minutes"
    entry_price: float
    stop_loss: float
    target_1: float
    target_2: float
    risk_reward_ratio: float
    volume_confirmed: bool
    order_block_high: float
    order_block_low: float
    current_price: float
    signal_strength: str            # "Strong" / "Medium" / "Weak"
    breakout_candle_time: str       # ISO timestamp, used as the dedup key
    reason: str = ""

    def dedup_key(self) -> str:
        """Uniquely identifies this exact signal occurrence — same symbol,
        same direction, same confirming candle. Used to guarantee a given
        breakout is never reported twice."""
        return f"{self.symbol}|{self.direction}|{self.breakout_candle_time}"

    def as_row(self) -> dict:
        return asdict(self)


# ══════════════════════════════════════════════════════════════════════════
# ── 6. RESILIENT FYERS HISTORY FETCH ─────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════

def safe_history(
    fyers,
    params: dict,
    max_retries: int = _HISTORY_MAX_RETRIES,
    base_delay: float = _HISTORY_BASE_DELAY_SECONDS,
) -> Tuple[Optional[dict], Optional[str]]:
    """Calls fyers.history(params) with retries + exponential-ish backoff.

    Returns (response_dict, None) on success, or (None, short_error) if
    every retry is exhausted or the symbol/params are rejected outright
    (in which case retrying would not help)."""
    symbol = params.get("symbol", "UNKNOWN")
    last_err = "unknown error"

    for attempt in range(1, max_retries + 1):
        try:
            resp = fyers.history(params)
        except Exception as exc:  # network, timeout, malformed JSON, etc.
            last_err = f"{type(exc).__name__}: {exc}"
        else:
            if not isinstance(resp, dict):
                last_err = "empty/invalid response"
            else:
                status = resp.get("s")
                if status == "ok":
                    candles = resp.get("candles")
                    if isinstance(candles, list):
                        return resp, None
                    last_err = "malformed candle data"
                else:
                    message = str(resp.get("message", status or "unknown"))
                    if "rate" in message.lower() or "limit" in message.lower():
                        last_err = f"rate limited: {message}"
                        time.sleep(base_delay * attempt * 2)
                        continue
                    # Invalid/delisted symbol — retrying will not help.
                    logger.warning("Symbol rejected by FYERS: %s (%s)", symbol, message)
                    return None, message

        if attempt < max_retries:
            time.sleep(base_delay * attempt)

    logger.error("History fetch failed for %s after %d attempts: %s", symbol, max_retries, last_err)
    return None, f"{symbol}: {last_err} (after {max_retries} attempts)"


def fetch_15m_candles(fyers, symbol: str, lookback_days: int = HISTORY_LOOKBACK_DAYS) -> Tuple[Optional[pd.DataFrame], Optional[str]]:
    """Fetches and cleans 15-minute OHLCV candles for `symbol`. Returns
    (DataFrame, None) on success or (None, error_message) on failure.

    The DataFrame is always sorted ascending by time and has a tz-aware
    'Time' column in Asia/Kolkata."""
    date_from = (datetime.today() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    date_to = datetime.today().strftime("%Y-%m-%d")

    resp, err = safe_history(fyers, {
        "symbol": symbol,
        "resolution": RESOLUTION,
        "date_format": "1",
        "range_from": date_from,
        "range_to": date_to,
        "cont_flag": "1",
    })
    if err:
        return None, err

    candles = resp.get("candles") if resp else None
    if not candles or len(candles) < (SWING_LOOKBACK_CANDLES + ORDER_BLOCK_SEARCH_WINDOW + 5):
        return None, f"{symbol}: insufficient 15-min history ({len(candles) if candles else 0} candles)"

    try:
        df = pd.DataFrame(candles, columns=["Time", "Open", "High", "Low", "Close", "Volume"])
        df["Time"] = pd.to_datetime(df["Time"], unit="s", utc=True).dt.tz_convert(IST)
        numeric_cols = ["Open", "High", "Low", "Close", "Volume"]
        df[numeric_cols] = df[numeric_cols].apply(pd.to_numeric, errors="coerce")
        df = df.dropna(subset=numeric_cols).sort_values("Time").reset_index(drop=True)
    except (KeyError, ValueError, TypeError) as exc:
        return None, f"{symbol}: malformed candle data ({exc})"

    # Drop a still-forming (not yet closed) final candle, if present, so
    # detection never runs against an incomplete candle.
    if len(df) > 0 and not is_candle_closed(df["Time"].iloc[-1]):
        df = df.iloc[:-1].reset_index(drop=True)

    if len(df) < (SWING_LOOKBACK_CANDLES + ORDER_BLOCK_SEARCH_WINDOW + 5):
        return None, f"{symbol}: insufficient closed 15-min candles after trimming"

    return df, None


# ══════════════════════════════════════════════════════════════════════════
# ── 7. SYMBOL UNIVERSE (optional convenience default) ───────────────────
# ══════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=60 * 60 * 12)
def load_default_nse_symbols(limit: int = 100) -> List[str]:
    """Downloads FYERS' NSE Capital Market symbol master and returns up to
    `limit` NSE equity ('NSE:SYMBOL-EQ') symbols. This is only a fallback
    for when app.py calls run_dashboard(fyers) without an explicit symbol
    list — for production scanning, pass your own curated/watchlist
    symbols instead for speed and relevance."""
    import requests

    try:
        resp = requests.get(FYERS_NSE_CM_SYMBOL_MASTER, timeout=20)
        resp.raise_for_status()
    except Exception as exc:
        logger.error("Could not download FYERS symbol master: %s", exc)
        return []

    symbols: List[str] = []
    for line in resp.text.strip().split("\n"):
        parts = line.split(",")
        for part in parts:
            part = part.strip()
            if part.startswith("NSE:") and part.endswith("-EQ"):
                symbols.append(part)
                break
        if len(symbols) >= limit:
            break

    return sorted(set(symbols))


# ══════════════════════════════════════════════════════════════════════════
# ── 8. ORDER BLOCK DETECTION ENGINE ──────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════

def _find_last_opposite_candle(df: pd.DataFrame, breakout_idx: int, bullish_ob: bool) -> Optional[int]:
    """Scans backwards from just before the breakout candle (within
    ORDER_BLOCK_SEARCH_WINDOW candles) for the LAST candle of the opposite
    colour to the breakout — i.e. the actual "order block" candle.

    bullish_ob=True  -> look for the last BEARISH candle (Close < Open)
    bullish_ob=False -> look for the last BULLISH candle (Close > Open)
    """
    start = max(0, breakout_idx - ORDER_BLOCK_SEARCH_WINDOW)
    for i in range(breakout_idx - 1, start - 1, -1):
        candle = df.iloc[i]
        is_bearish = candle["Close"] < candle["Open"]
        is_bullish = candle["Close"] > candle["Open"]
        if bullish_ob and is_bearish:
            return i
        if not bullish_ob and is_bullish:
            return i
    return None


def _signal_strength(
    volume_ratio: float, confirmation_strength_pct: float, rr_ratio: float
) -> str:
    """Combines volume conviction, how decisively the confirmation candle
    cleared the breakout level, and risk/reward quality into a simple
    Strong / Medium / Weak label."""
    score = 0
    if volume_ratio >= 2.0:
        score += 2
    elif volume_ratio >= VOLUME_CONFIRMATION_MULTIPLIER:
        score += 1

    if confirmation_strength_pct >= 0.5:
        score += 2
    elif confirmation_strength_pct >= 0.15:
        score += 1

    if rr_ratio >= 2.0:
        score += 2
    elif rr_ratio >= 1.2:
        score += 1

    if score >= 5:
        return "Strong"
    if score >= 3:
        return "Medium"
    return "Weak"


def detect_order_block_signal(df: pd.DataFrame, symbol: str) -> Optional[OrderBlockSignal]:
    """Runs the full Bullish/Bearish Order Block rule set against the most
    recently CLOSED 15-minute candles for `symbol` and returns a fresh
    OrderBlockSignal if (and only if) every rule is satisfied on the very
    latest candle (the "confirmation candle"). Returns None if no fresh
    signal is present right now — that is the normal, common case.

    Bullish Order Block rules
    -------------------------
    1. A breakout candle closes above the prior swing high (the highest
       high of the SWING_LOOKBACK_CANDLES candles before it).
    2. That breakout candle's volume exceeds VOLUME_CONFIRMATION_MULTIPLIER
       times the 20-candle average volume.
    3. The very next candle (the confirmation candle == the latest closed
       candle) ALSO closes above the breakout level.
    4. The Order Block itself is the last BEARISH candle before the
       breakout candle (its High/Low define the OB zone).

    Bearish Order Block rules are the exact mirror image (breakdown below
    prior swing low, last BULLISH candle before it is the OB, confirmation
    candle closes below the breakdown level).
    """
    if len(df) < SWING_LOOKBACK_CANDLES + ORDER_BLOCK_SEARCH_WINDOW + 5:
        return None

    d = df.reset_index(drop=True)
    d["Prior_Swing_High"] = d["High"].rolling(SWING_LOOKBACK_CANDLES).max().shift(1)
    d["Prior_Swing_Low"] = d["Low"].rolling(SWING_LOOKBACK_CANDLES).min().shift(1)
    d["Vol_Avg20"] = d["Volume"].rolling(VOLUME_AVG_WINDOW).mean()

    confirm_idx = len(d) - 1          # latest CLOSED candle == confirmation candle
    breakout_idx = confirm_idx - 1    # the candle immediately before it
    if breakout_idx < SWING_LOOKBACK_CANDLES:
        return None

    confirm_candle = d.iloc[confirm_idx]
    breakout_candle = d.iloc[breakout_idx]

    if pd.isna(breakout_candle["Prior_Swing_High"]) or pd.isna(breakout_candle["Prior_Swing_Low"]) \
            or pd.isna(breakout_candle["Vol_Avg20"]) or breakout_candle["Vol_Avg20"] <= 0:
        return None

    vol_ratio = float(breakout_candle["Volume"] / breakout_candle["Vol_Avg20"])
    volume_ok = vol_ratio >= VOLUME_CONFIRMATION_MULTIPLIER

    # ── Try Bullish Order Block first ───────────────────────────────────
    breakout_up = breakout_candle["Close"] > breakout_candle["Prior_Swing_High"]
    confirm_up = confirm_candle["Close"] > breakout_candle["Prior_Swing_High"]

    if breakout_up and volume_ok and confirm_up:
        ob_idx = _find_last_opposite_candle(d, breakout_idx, bullish_ob=True)
        if ob_idx is not None:
            ob_candle = d.iloc[ob_idx]
            ob_high = float(ob_candle["High"])
            ob_low = float(ob_candle["Low"])
            current_price = float(confirm_candle["Close"])
            entry_price = current_price
            stop_loss = round(ob_low, 2)
            zone_height = max(ob_high - ob_low, entry_price * 0.001)
            target_1 = round(entry_price + TARGET1_RR_MULTIPLE * zone_height, 2)
            target_2 = round(entry_price + TARGET2_RR_MULTIPLE * zone_height, 2)
            risk = abs(entry_price - stop_loss)
            reward = abs(target_1 - entry_price)
            rr_ratio = round(reward / risk, 2) if risk > 0 else 0.0

            breakout_level = float(breakout_candle["Prior_Swing_High"])
            confirm_strength_pct = (
                (confirm_candle["Close"] - breakout_level) / breakout_level * 100
                if breakout_level else 0.0
            )
            strength = _signal_strength(vol_ratio, confirm_strength_pct, rr_ratio)

            signal_date, signal_time = format_ist(confirm_candle["Time"])
            reason = (
                f"Bullish breakout above prior swing high {breakout_level:.2f} "
                f"(vol {vol_ratio:.2f}x avg), confirmed by next candle close "
                f"{confirm_candle['Close']:.2f}. Order Block from bearish candle at "
                f"{ob_candle['Time']}."
            )

            return OrderBlockSignal(
                symbol=symbol, direction="BUY",
                signal_date=signal_date, signal_time=signal_time,
                timeframe=TIMEFRAME_LABEL,
                entry_price=round(entry_price, 2), stop_loss=stop_loss,
                target_1=target_1, target_2=target_2, risk_reward_ratio=rr_ratio,
                volume_confirmed=volume_ok, order_block_high=round(ob_high, 2),
                order_block_low=round(ob_low, 2), current_price=round(current_price, 2),
                signal_strength=strength,
                breakout_candle_time=breakout_candle["Time"].isoformat(),
                reason=reason,
            )

    # ── Try Bearish Order Block ──────────────────────────────────────────
    breakdown_down = breakout_candle["Close"] < breakout_candle["Prior_Swing_Low"]
    confirm_down = confirm_candle["Close"] < breakout_candle["Prior_Swing_Low"]

    if breakdown_down and volume_ok and confirm_down:
        ob_idx = _find_last_opposite_candle(d, breakout_idx, bullish_ob=False)
        if ob_idx is not None:
            ob_candle = d.iloc[ob_idx]
            ob_high = float(ob_candle["High"])
            ob_low = float(ob_candle["Low"])
            current_price = float(confirm_candle["Close"])
            entry_price = current_price
            stop_loss = round(ob_high, 2)
            zone_height = max(ob_high - ob_low, entry_price * 0.001)
            target_1 = round(entry_price - TARGET1_RR_MULTIPLE * zone_height, 2)
            target_2 = round(entry_price - TARGET2_RR_MULTIPLE * zone_height, 2)
            risk = abs(stop_loss - entry_price)
            reward = abs(entry_price - target_1)
            rr_ratio = round(reward / risk, 2) if risk > 0 else 0.0

            breakdown_level = float(breakout_candle["Prior_Swing_Low"])
            confirm_strength_pct = (
                (breakdown_level - confirm_candle["Close"]) / breakdown_level * 100
                if breakdown_level else 0.0
            )
            strength = _signal_strength(vol_ratio, confirm_strength_pct, rr_ratio)

            signal_date, signal_time = format_ist(confirm_candle["Time"])
            reason = (
                f"Bearish breakdown below prior swing low {breakdown_level:.2f} "
                f"(vol {vol_ratio:.2f}x avg), confirmed by next candle close "
                f"{confirm_candle['Close']:.2f}. Order Block from bullish candle at "
                f"{ob_candle['Time']}."
            )

            return OrderBlockSignal(
                symbol=symbol, direction="SELL",
                signal_date=signal_date, signal_time=signal_time,
                timeframe=TIMEFRAME_LABEL,
                entry_price=round(entry_price, 2), stop_loss=stop_loss,
                target_1=target_1, target_2=target_2, risk_reward_ratio=rr_ratio,
                volume_confirmed=volume_ok, order_block_high=round(ob_high, 2),
                order_block_low=round(ob_low, 2), current_price=round(current_price, 2),
                signal_strength=strength,
                breakout_candle_time=breakout_candle["Time"].isoformat(),
                reason=reason,
            )

    return None


# ══════════════════════════════════════════════════════════════════════════
# ── 9. DE-DUPLICATION STORE ──────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════

def _load_dedup_state() -> Dict[str, str]:
    """Loads the persisted set of dedup keys already reported. Stored as a
    JSON object mapping dedup_key -> ISO timestamp first seen, so the file
    doubles as a lightweight audit trail."""
    ensure_directories()
    if not DEDUP_STATE_PATH.exists():
        return {}
    try:
        with open(DEDUP_STATE_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read dedup state (%s) — starting fresh.", exc)
        return {}


def _save_dedup_state(state: Dict[str, str]) -> None:
    ensure_directories()
    try:
        # Cap file growth: keep only the most recent 5000 entries.
        if len(state) > 5000:
            trimmed = dict(sorted(state.items(), key=lambda kv: kv[1])[-5000:])
            state = trimmed
        with open(DEDUP_STATE_PATH, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
    except OSError as exc:
        logger.error("Could not persist dedup state: %s", exc)


def filter_fresh_signals(signals: List[OrderBlockSignal]) -> List[OrderBlockSignal]:
    """Drops any signal whose dedup_key() has already been reported before
    (i.e. the same symbol+direction+confirming-candle combination), and
    records the newly-accepted ones so they are never reported again."""
    state = _load_dedup_state()
    fresh: List[OrderBlockSignal] = []
    for sig in signals:
        key = sig.dedup_key()
        if key not in state:
            fresh.append(sig)
            state[key] = now_ist().isoformat()
    if fresh:
        _save_dedup_state(state)
    return fresh


# ══════════════════════════════════════════════════════════════════════════
# ── 10. PERSISTENCE: TXT / CSV / JSONL ───────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════

def _safe_filename_component(text: str) -> str:
    return "".join(c if (c.isalnum() or c in ("-", "_")) else "_" for c in text)


def save_signal_txt(signal: OrderBlockSignal) -> Path:
    """Saves a human-readable signal file at
    signals/bullish/SYMBOL_DATE_TIME.txt or signals/bearish/... ."""
    ensure_directories()
    target_dir = SIGNALS_BULLISH_DIR if signal.direction == "BUY" else SIGNALS_BEARISH_DIR

    date_part = _safe_filename_component(signal.signal_date)
    time_part = _safe_filename_component(signal.signal_time.replace(":", "").replace(" ", "_"))
    filename = f"{signal.symbol}_{date_part}_{time_part}.txt"
    filepath = target_dir / filename

    lines = [
        f"Symbol            : {signal.symbol}",
        f"Signal            : {'BUY' if signal.direction == 'BUY' else 'SELL'}",
        f"Signal Time       : {signal.signal_date} {signal.signal_time}",
        f"Time Frame        : {signal.timeframe}",
        f"Entry Price       : {signal.entry_price}",
        f"Stop Loss         : {signal.stop_loss}",
        f"Target 1          : {signal.target_1}",
        f"Target 2          : {signal.target_2}",
        f"Risk Reward Ratio : {signal.risk_reward_ratio}",
        f"Volume Confirmed  : {'Yes' if signal.volume_confirmed else 'No'}",
        f"Order Block High  : {signal.order_block_high}",
        f"Order Block Low   : {signal.order_block_low}",
        f"Current Price     : {signal.current_price}",
        f"Signal Strength   : {signal.signal_strength}",
        f"Reason            : {signal.reason}",
    ]
    try:
        filepath.write_text("\n".join(lines), encoding="utf-8")
        logger.info("Saved signal file: %s", filepath)
    except OSError as exc:
        logger.error("Could not write signal file %s: %s", filepath, exc)
    return filepath


def append_csv_history(signal: OrderBlockSignal) -> None:
    ensure_directories()
    row = signal.as_row()
    file_exists = CSV_HISTORY_PATH.exists()
    try:
        with open(CSV_HISTORY_PATH, "a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(row.keys()))
            if not file_exists:
                writer.writeheader()
            writer.writerow(row)
    except OSError as exc:
        logger.error("Could not append to CSV history: %s", exc)


def append_jsonl_history(signal: OrderBlockSignal) -> None:
    ensure_directories()
    try:
        with open(JSONL_HISTORY_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(signal.as_row()) + "\n")
    except OSError as exc:
        logger.error("Could not append to JSONL history: %s", exc)


def persist_signal(signal: OrderBlockSignal) -> Path:
    """Runs all persistence steps for one fresh signal: txt file, CSV row,
    JSONL row, and a log line. Returns the saved .txt file path."""
    txt_path = save_signal_txt(signal)
    append_csv_history(signal)
    append_jsonl_history(signal)
    logger.info(
        "FRESH SIGNAL | %s | %s | Entry=%.2f SL=%.2f T1=%.2f T2=%.2f RR=%.2f Strength=%s",
        signal.symbol, signal.direction, signal.entry_price, signal.stop_loss,
        signal.target_1, signal.target_2, signal.risk_reward_ratio, signal.signal_strength,
    )
    return txt_path


# ══════════════════════════════════════════════════════════════════════════
# ── 11. CHART GENERATION (Plotly) ────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════

def generate_signal_chart(df: pd.DataFrame, signal: OrderBlockSignal) -> Optional[Path]:
    """Builds a candlestick + volume chart annotated with the Order Block
    zone, Entry, Stop Loss, and Target lines, and saves it under charts/.

    Tries to save a static PNG (requires the optional `kaleido` package);
    if that's unavailable, falls back to an interactive standalone HTML
    file instead so the chart is never silently skipped."""
    if not PLOTLY_AVAILABLE:
        logger.warning("Plotly not installed — skipping chart generation for %s.", signal.symbol)
        return None

    ensure_directories()
    plot_df = df.tail(80)  # last ~80 candles of 15-min context is plenty

    fig = go.Figure()
    fig.add_trace(go.Candlestick(
        x=plot_df["Time"], open=plot_df["Open"], high=plot_df["High"],
        low=plot_df["Low"], close=plot_df["Close"], name=signal.symbol,
        increasing_line_color="#26a69a", decreasing_line_color="#ef5350",
    ))

    # Order Block zone as a shaded rectangle.
    fig.add_shape(
        type="rect", xref="x", yref="y",
        x0=plot_df["Time"].iloc[0], x1=plot_df["Time"].iloc[-1],
        y0=signal.order_block_low, y1=signal.order_block_high,
        fillcolor="rgba(255,193,7,0.18)", line=dict(color="rgba(255,193,7,0.6)", width=1),
        layer="below",
    )
    fig.add_annotation(
        x=plot_df["Time"].iloc[-1], y=signal.order_block_high,
        text="Order Block Zone", showarrow=False, font=dict(size=10, color="#b8860b"),
        xanchor="right", yanchor="bottom",
    )

    level_lines = [
        ("Entry", signal.entry_price, "#2962ff"),
        ("Stop Loss", signal.stop_loss, "#d32f2f"),
        ("Target 1", signal.target_1, "#2e7d32"),
        ("Target 2", signal.target_2, "#1b5e20"),
    ]
    for label, price, color in level_lines:
        fig.add_hline(
            y=price, line=dict(color=color, width=1.5, dash="dash"),
            annotation_text=f"{label}: {price}", annotation_position="right",
            annotation_font=dict(color=color, size=10),
        )

    fig.add_trace(go.Bar(
        x=plot_df["Time"], y=plot_df["Volume"], name="Volume",
        marker_color="rgba(100,100,100,0.35)", yaxis="y2",
    ))

    fig.update_layout(
        title=f"{signal.symbol} — {signal.direction} Order Block ({signal.timeframe}) "
              f"@ {signal.signal_date} {signal.signal_time}",
        xaxis_rangeslider_visible=False,
        yaxis=dict(title="Price", domain=[0.25, 1.0]),
        yaxis2=dict(title="Volume", domain=[0.0, 0.2], anchor="x"),
        template="plotly_white",
        height=650,
        margin=dict(l=40, r=40, t=60, b=40),
        showlegend=False,
    )

    date_part = _safe_filename_component(signal.signal_date)
    time_part = _safe_filename_component(signal.signal_time.replace(":", "").replace(" ", "_"))
    base_name = f"{signal.symbol}_{signal.direction}_{date_part}_{time_part}"

    png_path = CHARTS_DIR / f"{base_name}.png"
    try:
        fig.write_image(str(png_path), scale=2)
        logger.info("Saved chart image: %s", png_path)
        return png_path
    except Exception as exc:
        # Most commonly: kaleido isn't installed. Fall back to HTML so the
        # chart is still produced and viewable.
        logger.warning("Static PNG export failed for %s (%s) — saving HTML instead.", signal.symbol, exc)
        html_path = CHARTS_DIR / f"{base_name}.html"
        try:
            fig.write_html(str(html_path), include_plotlyjs="cdn")
            logger.info("Saved chart HTML: %s", html_path)
            return html_path
        except OSError as exc2:
            logger.error("Could not save chart for %s: %s", signal.symbol, exc2)
            return None


# ══════════════════════════════════════════════════════════════════════════
# ── 12. SCAN ORCHESTRATION (multi-symbol, high performance) ─────────────
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class ScanStats:
    total: int
    scanned: int = 0
    signals_found: int = 0
    skipped: int = 0
    failed: int = 0
    elapsed_seconds: float = 0.0


def _scan_one_symbol(fyers, symbol: str) -> Tuple[Optional[OrderBlockSignal], Optional[pd.DataFrame], Optional[str]]:
    """Fetches 15-min candles for one symbol and runs Order Block
    detection. Returns (signal_or_None, dataframe_or_None, error_or_None).
    Never raises — every failure mode is caught and reduced to a short
    error string so one bad symbol can never crash the whole scan."""
    try:
        df, err = fetch_15m_candles(fyers, symbol)
        if err:
            return None, None, err
        signal = detect_order_block_signal(df, symbol)
        return signal, df, None
    except Exception as exc:  # absolute last-resort safety net
        logger.exception("Unexpected error scanning %s", symbol)
        return None, None, f"{symbol}: unexpected error ({type(exc).__name__})"


def run_order_block_scan(
    fyers, symbols: List[str], progress_callback=None,
) -> Tuple[List[OrderBlockSignal], Dict[str, pd.DataFrame], List[str], ScanStats]:
    """Threaded, rate-limited, high-performance scan across `symbols`.

    Returns:
        fresh_signals : de-duplicated list of brand-new OrderBlockSignal
        symbol_frames : {symbol: DataFrame} for every symbol that returned
                         usable candle data (used later for charting)
        errors        : short per-symbol error/skip messages
        stats         : ScanStats summary
    """
    start_time = time.time()
    stats = ScanStats(total=len(symbols))
    raw_signals: List[OrderBlockSignal] = []
    symbol_frames: Dict[str, pd.DataFrame] = {}
    errors: List[str] = []
    done = 0

    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i:i + BATCH_SIZE]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(_scan_one_symbol, fyers, s): s for s in batch}
            for future in as_completed(futures):
                symbol = futures[future]
                try:
                    signal, df, err = future.result()
                except Exception as exc:
                    signal, df, err = None, None, f"{symbol}: worker error ({type(exc).__name__})"

                if df is not None:
                    symbol_frames[symbol] = df
                if signal is not None:
                    raw_signals.append(signal)
                    stats.signals_found += 1
                if err:
                    errors.append(err)
                    stats.failed += 1
                elif signal is None:
                    stats.skipped += 1

                stats.scanned += 1
                done += 1
                if progress_callback is not None:
                    progress_callback(done, len(symbols))

        if i + BATCH_SIZE < len(symbols):
            time.sleep(BATCH_PAUSE_SECONDS)

    fresh_signals = filter_fresh_signals(raw_signals)
    stats.elapsed_seconds = round(time.time() - start_time, 1)

    logger.info(
        "Scan complete: total=%d scanned=%d fresh_signals=%d skipped=%d failed=%d elapsed=%.1fs",
        stats.total, stats.scanned, len(fresh_signals), stats.skipped, stats.failed, stats.elapsed_seconds,
    )
    return fresh_signals, symbol_frames, errors, stats


# ══════════════════════════════════════════════════════════════════════════
# ── 13. STREAMLIT DASHBOARD ───────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════

def _auto_refresh_control(interval_seconds: int) -> None:
    """Enables periodic dashboard refresh. Prefers the optional
    `streamlit-autorefresh` package (non-blocking, smooth); falls back to a
    simple blocking sleep + rerun cycle if that package isn't installed."""
    try:
        from streamlit_autorefresh import st_autorefresh  # type: ignore
        st_autorefresh(interval=interval_seconds * 1000, key="ob_scanner_autorefresh")
    except ImportError:
        st.caption(
            "ℹ️ Install `streamlit-autorefresh` for smoother auto-refresh "
            "(`pip install streamlit-autorefresh`). Using a basic fallback for now."
        )
        time.sleep(interval_seconds)
        st.rerun()


def _render_alert(signal: OrderBlockSignal) -> None:
    """Renders a live 🔔 alert block in the Streamlit UI for one fresh
    signal, in the exact format requested."""
    emoji = "🟢" if signal.direction == "BUY" else "🔴"
    label = "NEW BUY SIGNAL" if signal.direction == "BUY" else "NEW SELL SIGNAL"
    box = st.success if signal.direction == "BUY" else st.error
    box(
        f"🔔 **{label}**\n\n"
        f"**{signal.symbol}**\n\n"
        f"Time : {signal.signal_time}  \n"
        f"Time Frame : {signal.timeframe}  \n"
        f"Entry : {signal.entry_price}  \n"
        f"Stoploss : {signal.stop_loss}  \n"
        f"Target1 : {signal.target_1}  \n"
        f"Target2 : {signal.target_2}  \n"
        f"{emoji} Signal Strength: {signal.signal_strength} | RR: {signal.risk_reward_ratio}"
    )


def _signals_to_dataframe(signals: List[OrderBlockSignal]) -> pd.DataFrame:
    if not signals:
        return pd.DataFrame()
    rows = []
    for s in signals:
        rows.append({
            "Symbol": s.symbol,
            "Signal": "🟢 BUY" if s.direction == "BUY" else "🔴 SELL",
            "Signal Time": f"{s.signal_date} {s.signal_time}",
            "Time Frame": s.timeframe,
            "Entry Price": s.entry_price,
            "Stop Loss": s.stop_loss,
            "Target 1": s.target_1,
            "Target 2": s.target_2,
            "Risk Reward Ratio": s.risk_reward_ratio,
            "Volume Confirmation": "✅ Yes" if s.volume_confirmed else "❌ No",
            "Order Block High": s.order_block_high,
            "Order Block Low": s.order_block_low,
            "Current Price": s.current_price,
            "Signal Strength": s.signal_strength,
        })
    return pd.DataFrame(rows)


def run_dashboard(fyers, symbols: Optional[List[str]] = None) -> None:
    """Main Streamlit entry point. Call this directly from app.py:

        import scanner
        scanner.run_dashboard(fyers)

    `symbols` is an optional explicit list of 'NSE:SYMBOL-EQ' strings —
    if omitted, a default universe is pulled from FYERS' public NSE_CM
    symbol master (capped for scan speed; adjust in the sidebar).
    """
    ensure_directories()
    st.title("🎯 NSE 15-Min Order Block Scanner")
    st.caption(f"🕒 Current Time (IST): {now_ist().strftime('%d-%b-%Y %H:%M:%S')} IST — Timeframe: {TIMEFRAME_LABEL}")

    # ── Sidebar controls ────────────────────────────────────────────────
    with st.sidebar:
        st.header("⚙️ Scanner Controls")
        auto_refresh_enabled = st.checkbox("Enable auto-refresh", value=True)
        refresh_interval = st.number_input(
            "Refresh interval (seconds)", min_value=10, max_value=300,
            value=DEFAULT_REFRESH_SECONDS, step=5,
        )
        symbol_limit = st.number_input(
            "Symbol scan limit (default universe only)", min_value=10, max_value=500,
            value=100, step=10,
            help="Only applies when no explicit `symbols` list is passed to run_dashboard().",
        )
        show_charts = st.checkbox("Generate charts for fresh signals", value=True)
        st.divider()
        st.caption(
            "Rules: Bullish OB = last bearish candle before a breakout candle "
            "closing above the prior swing high with volume ≥ 20-candle "
            "average, confirmed by the next candle closing above the "
            "breakout level. Bearish OB is the mirror image."
        )

    # ── Resolve symbol universe ─────────────────────────────────────────
    if symbols is None:
        with st.spinner("Loading default NSE symbol universe…"):
            universe = load_default_nse_symbols(limit=int(symbol_limit))
    else:
        universe = list(symbols)

    if not universe:
        st.warning("No symbols available to scan — pass `symbols=[...]` to run_dashboard() or check network access.")
        return

    st.caption(f"Scanning {len(universe)} symbols on {TIMEFRAME_LABEL} candles.")

    # ── Run scan ─────────────────────────────────────────────────────────
    progress_bar = st.progress(0.0, text="Starting scan…")

    def _progress(done: int, total: int) -> None:
        progress_bar.progress(done / total, text=f"Scanning {done} / {total}")

    with st.spinner("Fetching 15-minute candles and detecting Order Blocks…"):
        fresh_signals, symbol_frames, errors, stats = run_order_block_scan(fyers, universe, _progress)
    progress_bar.empty()

    # ── Persist + chart every fresh signal ──────────────────────────────
    chart_paths: Dict[str, Path] = {}
    for signal in fresh_signals:
        persist_signal(signal)
        if show_charts and signal.symbol in symbol_frames:
            path = generate_signal_chart(symbol_frames[signal.symbol], signal)
            if path is not None:
                chart_paths[signal.dedup_key()] = path

    # ── Scan summary ─────────────────────────────────────────────────────
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Total Symbols", stats.total)
    c2.metric("Scanned", stats.scanned)
    c3.metric("Fresh Signals", len(fresh_signals))
    c4.metric("Skipped/No Signal", stats.skipped)
    c5.metric("Failed", stats.failed)
    st.caption(f"Scan completed in {stats.elapsed_seconds}s.")

    # ── Live alerts ──────────────────────────────────────────────────────
    st.subheader("🔔 Live Alerts")
    if fresh_signals:
        for signal in fresh_signals:
            _render_alert(signal)
    else:
        st.info("No fresh Order Block signals this cycle. The dashboard will keep scanning on refresh.")

    # ── Signals table ────────────────────────────────────────────────────
    st.subheader("📋 Fresh Signals — This Scan")
    df_signals = _signals_to_dataframe(fresh_signals)
    if not df_signals.empty:
        st.dataframe(df_signals, use_container_width=True, height=350)
        st.download_button(
            "📥 Download this scan's signals as CSV",
            data=df_signals.to_csv(index=False).encode("utf-8"),
            file_name=f"order_block_signals_{now_ist().strftime('%Y%m%d_%H%M')}.csv",
            mime="text/csv",
        )
    else:
        st.caption("Nothing to show yet — no fresh signals in this scan cycle.")

    # ── Charts ───────────────────────────────────────────────────────────
    if show_charts and chart_paths:
        st.subheader("📈 Signal Charts")
        for signal in fresh_signals:
            path = chart_paths.get(signal.dedup_key())
            if path is None:
                continue
            st.markdown(f"**{signal.symbol} — {signal.direction}** ({signal.signal_date} {signal.signal_time})")
            if path.suffix == ".png":
                st.image(str(path), use_container_width=True)
            else:
                with open(path, "r", encoding="utf-8") as fh:
                    st.components.v1.html(fh.read(), height=650, scrolling=True)

    # ── Historical signal log viewer ─────────────────────────────────────
    with st.expander("📚 Full Signal History (CSV log)"):
        if CSV_HISTORY_PATH.exists():
            hist_df = pd.read_csv(CSV_HISTORY_PATH)
            st.dataframe(hist_df.tail(200), use_container_width=True, height=400)
        else:
            st.caption("No signal history recorded yet.")

    # ── Errors/skips ─────────────────────────────────────────────────────
    if errors:
        with st.expander(f"⚠️ Skipped/failed symbols ({len(errors)})"):
            st.caption("Most entries are simply skipped for insufficient/invalid data, not app errors.")
            st.text("\n".join(errors[:30]))

    # ── Auto-refresh (kept last so everything above renders first) ──────
    if auto_refresh_enabled:
        _auto_refresh_control(int(refresh_interval))


# ══════════════════════════════════════════════════════════════════════════
# ── Standalone smoke-test entry point ────────────────────────────────────
# Running `streamlit run scanner.py` directly (without a real FYERS session
# from app.py) will show a friendly message instead of crashing, since a
# live `fyers` client is required for real scanning.
# ══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    st.title("🎯 NSE 15-Min Order Block Scanner")
    st.warning(
        "This module is designed to be imported from `app.py`, which owns the "
        "FYERS API V3 login/session and passes the authenticated `fyers` "
        "client into `scanner.run_dashboard(fyers)`.\n\n"
        "Example app.py:\n\n"
        "```python\n"
        "from fyers_apiv3 import fyersModel\n"
        "import scanner\n\n"
        "fyers = fyersModel.FyersModel(\n"
        "    client_id=CLIENT_ID, token=ACCESS_TOKEN, is_async=False, log_path=''\n"
        ")\n"
        "scanner.run_dashboard(fyers, symbols=['NSE:RELIANCE-EQ', 'NSE:TCS-EQ'])\n"
        "```"
    )
