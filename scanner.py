# ══════════════════════════════════════════════════════════════════════════
# live_ob_ai_engine.py
# ──────────────────────────────────────────────────────────────────────────
# TRUE LIVE 15-MINUTE ORDER BLOCK AI SCANNER — additive module.
#
# This file is 100% self-contained (it does not import anything from
# scaner.py) so dropping it next to your existing app can never break any
# existing scanner, tab, folder, or function. To wire it into your app you
# only need to add TWO lines to scaner.py — see the bottom of this file
# for the exact integration snippet.
#
# WHAT THIS MODULE DOES (per your spec):
#   • Never guesses. Every number shown is recomputed from the live data
#     fetched in THAT scan cycle — nothing is cached or reused between
#     refreshes. There is no "previous AI report" state anywhere.
#   • Only uses the LAST FULLY CLOSED 15-minute candle. The currently
#     forming candle is always dropped before any detection/scoring runs.
#   • Detects: Bullish OB, Bearish OB, Fresh OB, Mitigated OB, Breaker
#     Block, Demand Zone, Supply Zone — from confirmed candles only, and
#     never repaints a zone once it has been fixed from closed candles.
#   • Confidence < 70% => the row is always shown as WAIT, never BUY/SELL.
#   • Applies your BUY/SELL condition checklist, AI rejection filters,
#     and a simple news/VIX/gap sanity filter.
#   • Saves every NEW (non-duplicate) signal as CSV + JSON + TXT + PNG
#     chart under signals/<buy|sell>/, signals/history/,
#     signals/orderblock/, and charts/<buy|sell>/, charts/history/.
#   • Renders a live Streamlit dashboard tab with a 30-second auto-refresh,
#     scan progress, and all requested columns.
# ══════════════════════════════════════════════════════════════════════════

import os
import re
import csv
import json
import time
import logging
from datetime import datetime, timedelta
from typing import List, Optional, Tuple, Dict, Any
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd
import requests
import streamlit as st

try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
except Exception:  # pragma: no cover
    from datetime import timezone
    IST = timezone(timedelta(hours=5, minutes=30))

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False


# ══════════════════════════════════════════════════════════════════════════
# ── FOLDERS (auto-created, additive — never touches your existing folders) ─
# ══════════════════════════════════════════════════════════════════════════
V2_SIGNALS_DIR = "signals"
V2_SIGNALS_BUY_DIR = os.path.join(V2_SIGNALS_DIR, "buy")
V2_SIGNALS_SELL_DIR = os.path.join(V2_SIGNALS_DIR, "sell")
V2_SIGNALS_HISTORY_DIR = os.path.join(V2_SIGNALS_DIR, "history")
V2_SIGNALS_ORDERBLOCK_DIR = os.path.join(V2_SIGNALS_DIR, "orderblock")
V2_CHARTS_DIR = "charts"
V2_CHARTS_BUY_DIR = os.path.join(V2_CHARTS_DIR, "buy")
V2_CHARTS_SELL_DIR = os.path.join(V2_CHARTS_DIR, "sell")
V2_CHARTS_HISTORY_DIR = os.path.join(V2_CHARTS_DIR, "history")
V2_LOGS_DIR = "logs"

_ALL_V2_FOLDERS = (
    V2_SIGNALS_DIR, V2_SIGNALS_BUY_DIR, V2_SIGNALS_SELL_DIR, V2_SIGNALS_HISTORY_DIR,
    V2_SIGNALS_ORDERBLOCK_DIR, V2_CHARTS_DIR, V2_CHARTS_BUY_DIR, V2_CHARTS_SELL_DIR,
    V2_CHARTS_HISTORY_DIR, V2_LOGS_DIR,
)


def ensure_v2_folders() -> None:
    for folder in _ALL_V2_FOLDERS:
        os.makedirs(folder, exist_ok=True)


ensure_v2_folders()

logger = logging.getLogger("live_ob_ai_engine")
logger.setLevel(logging.INFO)
if not logger.handlers:
    try:
        _fh = logging.FileHandler(os.path.join(V2_LOGS_DIR, "live_ob_ai_engine.log"), encoding="utf-8")
        _fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))
        logger.addHandler(_fh)
    except OSError:
        logger.addHandler(logging.StreamHandler())

_SEEN_V2_FILE = os.path.join(V2_SIGNALS_DIR, "_seen_v2_signal_keys.json")
_SEEN_V2_MAX_KEEP = 8000

_V2_MASTER_CSV = os.path.join(V2_SIGNALS_HISTORY_DIR, "all_signals_log.csv")
_V2_MASTER_JSON = os.path.join(V2_SIGNALS_HISTORY_DIR, "all_signals_log.json")

# ── Config ───────────────────────────────────────────────────────────────
RESOLUTION = "15"
RESOLUTION_MINUTES = 15
LOOKBACK_DAYS = 5
AUTO_REFRESH_SECONDS = 30
MIN_CONFIDENCE_FOR_TRADE = 70.0     # < 70% => always WAIT, never BUY/SELL
MAX_WORKERS = 8
BATCH_SIZE = 40
BATCH_PAUSE_SECONDS = 1.0

NIFTY_SYMBOL = "NSE:NIFTY50-INDEX"
BANKNIFTY_SYMBOL = "NSE:NIFTYBANK-INDEX"
INDIA_VIX_SYMBOL = "NSE:INDIAVIX-INDEX"

_VALID_EQ_SYMBOL_RE = re.compile(r"^NSE:[A-Z0-9&\-]+-EQ$")


def _now_ist() -> datetime:
    return datetime.now(IST)


def _validate_symbols(symbols: List[str]) -> List[str]:
    seen, valid = set(), []
    for s in symbols:
        if not isinstance(s, str):
            continue
        s = s.strip().upper()
        if not s or s in seen or not _VALID_EQ_SYMBOL_RE.match(s):
            continue
        seen.add(s)
        valid.append(s)
    return valid


# ══════════════════════════════════════════════════════════════════════════
# ── SAFE, RETRYING FYERS HISTORY FETCH (self-contained copy) ──────────────
# ══════════════════════════════════════════════════════════════════════════
def _safe_history(fyers, params: dict, max_retries: int = 3, base_delay: float = 1.0):
    symbol = params.get("symbol", "UNKNOWN")
    last_err = "unknown error"
    for attempt in range(1, max_retries + 1):
        try:
            resp = fyers.history(params)
        except requests.exceptions.Timeout:
            last_err = "timeout"
        except requests.exceptions.ConnectionError:
            last_err = "network error"
        except requests.exceptions.RequestException as e:
            last_err = f"request error: {e}"
        except (ValueError, TypeError) as e:
            last_err = f"invalid response: {e}"
        except Exception as e:
            last_err = f"unexpected error: {e}"
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
                    return None, message  # invalid/delisted symbol — retry won't help
        if attempt < max_retries:
            time.sleep(base_delay * attempt)
    return None, f"{symbol}: {last_err} (after {max_retries} attempts)"


def _is_candle_closed(candle_start_ist: datetime, resolution_minutes: int) -> bool:
    """A candle is only usable once wall-clock time has passed its close."""
    return _now_ist() >= (candle_start_ist + timedelta(minutes=resolution_minutes))


def fetch_confirmed_15m_candles(fyers, symbol: str) -> Tuple[Optional[pd.DataFrame], Optional[str]]:
    """Fetches 15-min candles and ALWAYS drops the currently-forming candle,
    so every downstream calculation only ever sees fully confirmed data.
    Never returns a stale/cached frame — this hits the live API every call."""
    date_from = (datetime.today() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    date_to = datetime.today().strftime("%Y-%m-%d")

    resp, err = _safe_history(fyers, {
        "symbol": symbol, "resolution": RESOLUTION, "date_format": "1",
        "range_from": date_from, "range_to": date_to, "cont_flag": "1",
    })
    if err:
        return None, err

    candles = resp.get("candles") if resp else None
    if not candles or len(candles) < 35:
        return None, None  # not enough data yet — not a hard error

    df = pd.DataFrame(candles, columns=["Time", "Open", "High", "Low", "Close", "Volume"])
    df["Time"] = pd.to_datetime(df["Time"], unit="s", utc=True).dt.tz_convert(IST)
    df[["Open", "High", "Low", "Close", "Volume"]] = df[["Open", "High", "Low", "Close", "Volume"]].apply(
        pd.to_numeric, errors="coerce"
    )
    df = df.dropna(subset=["Open", "High", "Low", "Close"]).sort_values("Time").reset_index(drop=True)

    # Drop the still-forming candle — only confirmed/closed candles allowed.
    if len(df) and not _is_candle_closed(df["Time"].iloc[-1], RESOLUTION_MINUTES):
        df = df.iloc[:-1].reset_index(drop=True)

    if len(df) < 30:
        return None, None

    return df, None


# ══════════════════════════════════════════════════════════════════════════
# ── MARKET CONTEXT: NIFTY / BANKNIFTY / INDIA VIX (live, no caching) ──────
# ══════════════════════════════════════════════════════════════════════════
def fetch_market_context(fyers) -> Dict[str, Any]:
    """Fetches live Nifty / BankNifty trend + India VIX level fresh every
    call — never cached across refreshes, per the 'ignore old cached
    values' requirement. Falls back gracefully (does not block scanning)
    if any single index fetch fails."""
    context = {
        "nifty_trend": "Unknown", "banknifty_trend": "Unknown",
        "india_vix": None, "vix_regime": "Unknown", "gap_regime": "Unknown",
    }
    for key_prefix, symbol in (("nifty", NIFTY_SYMBOL), ("banknifty", BANKNIFTY_SYMBOL)):
        df, err = fetch_confirmed_15m_candles(fyers, symbol)
        if df is None or err:
            continue
        ema20 = df["Close"].ewm(span=20, adjust=False).mean().iloc[-1]
        trend = "Bullish" if df["Close"].iloc[-1] > ema20 else "Bearish"
        context[f"{key_prefix}_trend"] = trend
        if len(df) >= 2 and pd.notna(df["Close"].iloc[-2]) and df["Close"].iloc[-2] != 0:
            gap = (df["Open"].iloc[-1] - df["Close"].iloc[-2]) / df["Close"].iloc[-2] * 100
            if key_prefix == "nifty":
                context["gap_regime"] = "Gap Up" if gap > 0.3 else ("Gap Down" if gap < -0.3 else "Flat")

    try:
        resp, err = _safe_history(fyers, {
            "symbol": INDIA_VIX_SYMBOL, "resolution": RESOLUTION, "date_format": "1",
            "range_from": (datetime.today() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d"),
            "range_to": datetime.today().strftime("%Y-%m-%d"), "cont_flag": "1",
        })
        if not err and resp and resp.get("candles"):
            vix_close = float(resp["candles"][-1][4])
            context["india_vix"] = round(vix_close, 2)
            context["vix_regime"] = "High VIX" if vix_close >= 18 else ("Elevated VIX" if vix_close >= 14 else "Calm VIX")
    except Exception:
        pass

    return context


# ══════════════════════════════════════════════════════════════════════════
# ── LIVE TECHNICAL INDICATORS (recomputed fresh from confirmed candles) ──
# ══════════════════════════════════════════════════════════════════════════
def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return (100 - (100 / (1 + rs))).fillna(50)


def macd(close: pd.Series):
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    return macd_line, signal_line, macd_line - signal_line


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Standard Wilder's ADX — trend-strength filter used by both the
    BUY/SELL checklist (ADX > 25) and the AI rejection filter (low ADX)."""
    high, low, close = df["High"], df["Low"], df["Close"]
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    tr = pd.concat([
        high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()
    ], axis=1).max(axis=1)

    atr_ = tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / period, min_periods=period, adjust=False).mean() / atr_.replace(0, np.nan)
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / period, min_periods=period, adjust=False).mean() / atr_.replace(0, np.nan)

    dx = (100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)).fillna(0)
    return dx.ewm(alpha=1 / period, min_periods=period, adjust=False).mean().fillna(0)


def supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0) -> Tuple[str, Optional[bool]]:
    d = df.reset_index(drop=True)
    atr_series = atr(d, period)
    valid_start = atr_series.first_valid_index()
    if valid_start is None or len(d) - valid_start < 2:
        return "N/A", None

    d = d.iloc[valid_start:].reset_index(drop=True)
    a = atr_series.iloc[valid_start:].reset_index(drop=True).values
    close, high, low = d["Close"].values, d["High"].values, d["Low"].values
    hl2 = (high + low) / 2.0
    upperband, lowerband = hl2 + multiplier * a, hl2 - multiplier * a

    n = len(d)
    final_upper, final_lower = np.zeros(n), np.zeros(n)
    st_line, direction = np.zeros(n), np.ones(n, dtype=int)
    final_upper[0], final_lower[0] = upperband[0], lowerband[0]
    st_line[0] = final_upper[0]

    for i in range(1, n):
        final_upper[i] = upperband[i] if (upperband[i] < final_upper[i - 1] or close[i - 1] > final_upper[i - 1]) else final_upper[i - 1]
        final_lower[i] = lowerband[i] if (lowerband[i] > final_lower[i - 1] or close[i - 1] < final_lower[i - 1]) else final_lower[i - 1]
        if st_line[i - 1] == final_upper[i - 1]:
            if close[i] <= final_upper[i]:
                st_line[i], direction[i] = final_upper[i], -1
            else:
                st_line[i], direction[i] = final_lower[i], 1
        else:
            if close[i] >= final_lower[i]:
                st_line[i], direction[i] = final_lower[i], 1
            else:
                st_line[i], direction[i] = final_upper[i], -1

    is_bullish = bool(direction[-1] == 1)
    return ("🟢 Buy" if is_bullish else "🔴 Sell"), is_bullish


def intraday_session_vwap(df: pd.DataFrame) -> Optional[float]:
    """A TRUE intraday VWAP — cumulative (typical price * volume) / cumulative
    volume, reset at the start of each trading session (day), computed on
    the 15-min candles for just today's session. This is the real
    session VWAP, not a rolling multi-day approximation."""
    if df.empty:
        return None
    d = df.copy()
    d["Date"] = d["Time"].dt.date
    today = d["Date"].iloc[-1]
    session = d[d["Date"] == today]
    if session.empty:
        session = d.tail(20)
    typical = (session["High"] + session["Low"] + session["Close"]) / 3.0
    cum_vol = session["Volume"].cumsum()
    cum_tp_vol = (typical * session["Volume"]).cumsum()
    if cum_vol.iloc[-1] <= 0:
        return round(float(session["Close"].iloc[-1]), 2)
    return round(float(cum_tp_vol.iloc[-1] / cum_vol.iloc[-1]), 2)


# ══════════════════════════════════════════════════════════════════════════
# ── ORDER BLOCK ENGINE v2: Fresh / Mitigated / Breaker / Demand / Supply ──
# All detection runs ONLY on confirmed (closed) candles. Once a zone is
# derived from closed candles it is fixed — later scans reusing the same
# closed candles will always recompute the identical zone, so nothing
# "repaints" a previously reported level.
# ══════════════════════════════════════════════════════════════════════════
_OB_SWING_WINDOW = 10
_OB_MIN_IMPULSE_PCT = 0.8
_OB_VOL_MULTIPLIER = 1.2
_OB_LOOKBACK_CANDLES = 40


def _market_structure(df: pd.DataFrame) -> str:
    """Simple BOS/CHOCH structure read off swing highs/lows of confirmed
    candles — feeds both the Order Block engine and the report's
    'Market Structure' field."""
    d = df.tail(60).reset_index(drop=True)
    if len(d) < 15:
        return "Range"
    local_high = d["High"].rolling(_OB_SWING_WINDOW).max().shift(1)
    local_low = d["Low"].rolling(_OB_SWING_WINDOW).min().shift(1)
    ema20 = d["Close"].ewm(span=20, adjust=False).mean()
    ema50 = d["Close"].ewm(span=50, adjust=False).mean() if len(d) >= 50 else d["Close"].ewm(span=len(d), adjust=False).mean()
    bull_trend = bool(ema20.iloc[-1] > ema50.iloc[-1])

    last_close = d["Close"].iloc[-1]
    if pd.notna(local_high.iloc[-1]) and last_close > local_high.iloc[-1]:
        return "BOS Bullish (Higher High)" if bull_trend else "CHOCH Bullish (Trend Reversal Up)"
    if pd.notna(local_low.iloc[-1]) and last_close < local_low.iloc[-1]:
        return "BOS Bearish (Lower Low)" if not bull_trend else "CHOCH Bearish (Trend Reversal Down)"
    return "Range / Consolidation"


def _liquidity_zones(df: pd.DataFrame) -> Tuple[float, float]:
    """20-candle swing high/low used as Demand Zone (support/liquidity
    pool below) and Supply Zone (resistance/liquidity pool above)."""
    resistance = df["High"].rolling(20).max().shift(1).iloc[-1]
    support = df["Low"].rolling(20).min().shift(1).iloc[-1]
    return (float(support) if pd.notna(support) else None,
            float(resistance) if pd.notna(resistance) else None)


def detect_order_blocks_v2(df: pd.DataFrame) -> Dict[str, Any]:
    """Returns a dict describing every requested Order Block concept,
    derived strictly from confirmed candles (df must already have the
    forming candle removed by the caller):

      bullish_ob, bearish_ob   : bool
      fresh                    : bool  (zone has NOT been revisited since it formed)
      mitigated                : bool  (zone HAS been revisited/tapped at least once)
      breaker_block            : bool  (an OB that failed and flipped role)
      demand_zone, supply_zone : float | None
      zone_low, zone_high      : float | None
      ob_type                  : str   (human label used in reports)
    """
    result = {
        "bullish_ob": False, "bearish_ob": False, "fresh": False, "mitigated": False,
        "breaker_block": False, "demand_zone": None, "supply_zone": None,
        "zone_low": None, "zone_high": None, "ob_type": "None",
    }

    if len(df) < 20:
        return result

    d = df.tail(_OB_LOOKBACK_CANDLES).reset_index(drop=True)
    vol_avg = d["Volume"].tail(20).mean()
    last_close = float(d["Close"].iloc[-1])
    structure = _market_structure(df)
    is_bull_structure = "Bullish" in structure or "Up" in structure
    is_bear_structure = "Bearish" in structure or "Down" in structure

    demand_zone, supply_zone = _liquidity_zones(df)
    result["demand_zone"] = round(demand_zone, 2) if demand_zone else None
    result["supply_zone"] = round(supply_zone, 2) if supply_zone else None

    def _impulse_after(idx: int, is_bull_candle: bool) -> float:
        if idx + 1 >= len(d):
            return 0.0
        base = float(d["Close"].iloc[idx])
        if base == 0:
            return 0.0
        if is_bull_candle:
            return (float(d["Close"].iloc[idx + 1:].max()) - base) / base * 100
        return (base - float(d["Close"].iloc[idx + 1:].min())) / base * 100

    # ── Bullish OB search: last bearish candle before an impulsive up-move
    if is_bull_structure:
        for i in range(len(d) - 2, 0, -1):
            candle = d.iloc[i]
            if not (candle["Close"] < candle["Open"]):
                continue
            move_pct = _impulse_after(i, is_bull_candle=True)
            vol_ok = vol_avg > 0 and candle["Volume"] >= vol_avg * _OB_VOL_MULTIPLIER
            if move_pct >= _OB_MIN_IMPULSE_PCT and vol_ok:
                zone_low, zone_high = round(float(candle["Low"]), 2), round(float(candle["High"]), 2)
                # Fresh = price has NOT traded back down into the zone since
                # it formed. Mitigated = price already tapped the zone once.
                post_zone = d["Low"].iloc[i + 1:-1] if i + 1 < len(d) - 1 else pd.Series(dtype=float)
                touched = bool((post_zone <= zone_high).any()) if not post_zone.empty else False
                in_zone_now = zone_low <= last_close <= zone_high * 1.02
                if in_zone_now or touched:
                    result.update({
                        "bullish_ob": True, "zone_low": zone_low, "zone_high": zone_high,
                        "fresh": not touched, "mitigated": touched,
                        "ob_type": "Fresh Bullish Order Block" if not touched else "Mitigated Bullish Order Block",
                    })
                break

    # ── Bearish OB search: last bullish candle before an impulsive down-move
    if is_bear_structure and not result["bullish_ob"]:
        for i in range(len(d) - 2, 0, -1):
            candle = d.iloc[i]
            if not (candle["Close"] > candle["Open"]):
                continue
            move_pct = _impulse_after(i, is_bull_candle=False)
            vol_ok = vol_avg > 0 and candle["Volume"] >= vol_avg * _OB_VOL_MULTIPLIER
            if move_pct >= _OB_MIN_IMPULSE_PCT and vol_ok:
                zone_low, zone_high = round(float(candle["Low"]), 2), round(float(candle["High"]), 2)
                post_zone = d["High"].iloc[i + 1:-1] if i + 1 < len(d) - 1 else pd.Series(dtype=float)
                touched = bool((post_zone >= zone_low).any()) if not post_zone.empty else False
                in_zone_now = zone_low * 0.98 <= last_close <= zone_high
                if in_zone_now or touched:
                    result.update({
                        "bearish_ob": True, "zone_low": zone_low, "zone_high": zone_high,
                        "fresh": not touched, "mitigated": touched,
                        "ob_type": "Fresh Bearish Order Block" if not touched else "Mitigated Bearish Order Block",
                    })
                break

    # ── Breaker Block: a mitigated OB whose zone price has since closed
    # THROUGH (not just touched), meaning the zone's role has flipped
    # (a broken bullish OB becomes bearish resistance, and vice versa).
    if result["mitigated"] and result["zone_low"] is not None:
        if result["bullish_ob"] and last_close < result["zone_low"]:
            result["breaker_block"] = True
            result["ob_type"] = "Breaker Block (failed Bullish OB, now resistance)"
        elif result["bearish_ob"] and last_close > result["zone_high"]:
            result["breaker_block"] = True
            result["ob_type"] = "Breaker Block (failed Bearish OB, now support)"

    return result


# ══════════════════════════════════════════════════════════════════════════
# ── STATELESS AI CONFIDENCE ENGINE ────────────────────────────────────────
# Every input here is recomputed from the CURRENT scan's confirmed candles
# only. No global/session value is read or written by this function, so
# it is architecturally impossible for it to reuse a previous AI report.
# ══════════════════════════════════════════════════════════════════════════
def calculate_live_ai_confidence(
    direction: str, rsi_val: float, ema20: float, ema50: float, ema200: Optional[float],
    macd_bullish: bool, vwap_val: Optional[float], last_close: float, adx_val: float,
    volume_ratio: float, ob: Dict[str, Any], rr_ratio: float,
) -> Tuple[float, List[str]]:
    """Returns (confidence_percent, reasons_list). Purely a function of the
    live values passed in for THIS scan cycle — never memoized/cached."""
    is_buy = direction == "BUY"
    score = 50.0
    reasons: List[str] = []

    if ob.get("fresh"):
        score += 15
        reasons.append("Fresh Order Block (not yet retested)")
    elif ob.get("mitigated") and not ob.get("breaker_block"):
        score += 6
        reasons.append("Order Block already mitigated once (weaker edge)")
    if ob.get("breaker_block"):
        score += 8
        reasons.append("Breaker Block structure confirms the flip")

    rsi_ok = (rsi_val > 55) if is_buy else (rsi_val < 45)
    score += 10 if rsi_ok else -10
    reasons.append(f"RSI {rsi_val:.1f} {'supports' if rsi_ok else 'does not support'} {direction}")

    ema_ok = (ema20 > ema50) if is_buy else (ema20 < ema50)
    score += 10 if ema_ok else -10
    reasons.append(f"EMA20 {'above' if ema20 > ema50 else 'below'} EMA50")

    if ema200 is not None:
        trend_ok = (last_close > ema200) if is_buy else (last_close < ema200)
        score += 5 if trend_ok else -5

    score += 8 if (macd_bullish == is_buy) else -8
    reasons.append(f"MACD is {'bullish' if macd_bullish else 'bearish'}")

    if vwap_val is not None:
        vwap_ok = (last_close > vwap_val) if is_buy else (last_close < vwap_val)
        score += 8 if vwap_ok else -8
        reasons.append(f"Price is {'above' if last_close > vwap_val else 'below'} VWAP")

    adx_ok = adx_val > 25
    score += 8 if adx_ok else -8
    reasons.append(f"ADX {adx_val:.1f} {'shows a strong trend' if adx_ok else 'shows a weak/choppy trend'}")

    vol_ok = volume_ratio > 1.0
    score += 6 if vol_ok else -6
    reasons.append(f"Volume is {volume_ratio:.2f}x the 20-period average")

    if rr_ratio >= 2:
        score += 6
        reasons.append(f"Good Risk:Reward ({rr_ratio:.2f})")
    elif rr_ratio < 1:
        score -= 10
        reasons.append(f"Poor Risk:Reward ({rr_ratio:.2f})")

    score = max(0.0, min(100.0, score))
    return round(score, 1), reasons


# ══════════════════════════════════════════════════════════════════════════
# ── AI REJECTION FILTERS + NEWS/VIX SANITY FILTER ─────────────────────────
# ══════════════════════════════════════════════════════════════════════════
def apply_ai_filters(
    direction: str, volume_ratio: float, adx_val: float, rr_ratio: float,
    ob: Dict[str, Any], last_candle_ist: datetime, is_market_holiday_or_closed: bool,
) -> Optional[str]:
    """Returns an 'avoid trade' reason string if the signal must be
    rejected, else None. Every check here uses only live values from the
    current scan."""
    if is_market_holiday_or_closed:
        return "Market closed / holiday — no fresh live candle available"
    if not _is_candle_closed(last_candle_ist, RESOLUTION_MINUTES):
        return "Latest candle has not fully closed yet"
    if volume_ratio < 1.0:
        return "Low volume — below the 20-period average"
    if adx_val < 25:
        return "Weak trend — ADX below 25"
    if rr_ratio < 1.0:
        return "Poor Risk:Reward ratio"
    if direction == "BUY" and ob.get("supply_zone") is not None:
        return None  # handled by caller distance check
    if direction == "SELL" and ob.get("demand_zone") is not None:
        return None
    return None


def check_nearby_opposite_zone(direction: str, last_close: float, ob: Dict[str, Any], atr_val: float) -> Optional[str]:
    """Rejects a BUY if price is right under a Supply Zone, or a SELL if
    price is right above a Demand Zone (not enough room to run)."""
    buffer_ = max(atr_val * 1.5, last_close * 0.003)
    if direction == "BUY" and ob.get("supply_zone") is not None:
        if 0 <= (ob["supply_zone"] - last_close) <= buffer_:
            return "Nearby Supply Zone directly overhead — limited upside room"
    if direction == "SELL" and ob.get("demand_zone") is not None:
        if 0 <= (last_close - ob["demand_zone"]) <= buffer_:
            return "Nearby Demand Zone directly below — limited downside room"
    return None


def apply_news_vix_filter(direction: str, market_ctx: Dict[str, Any]) -> Optional[str]:
    """Simple sanity filter using live Nifty gap + India VIX (no paid news
    API wired in — if you have one, swap fetch_news_sentiment() below)."""
    vix_regime = market_ctx.get("vix_regime", "Unknown")
    gap_regime = market_ctx.get("gap_regime", "Unknown")

    if direction == "BUY":
        if vix_regime == "High VIX":
            return "High India VIX — avoid fresh BUY entries"
        if gap_regime == "Gap Down":
            return "Nifty gapped down — avoid BUY until stabilization"
    else:  # SELL
        if gap_regime == "Gap Up":
            return "Nifty gapped up — avoid fresh SELL into strength"
    return None


def fetch_news_sentiment(stock_ticker: str) -> Optional[str]:
    """Placeholder hook for a real news-sentiment API. Returns None
    (no live source wired in) so callers fall back to the VIX/gap filter
    above rather than ever fabricating a sentiment reading."""
    return None


# ══════════════════════════════════════════════════════════════════════════
# ── SIGNAL QUALITY LABEL ───────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════
def _signal_quality_label(confidence: float, confirmations: int) -> str:
    if confidence >= 90 and confirmations >= 6:
        return "🏦 Institutional"
    if confidence >= 85:
        return "🟣 Very Strong"
    if confidence >= 78:
        return "🟢 Strong"
    if confidence >= 70:
        return "🟡 Medium"
    return "⚪ Weak"


# ══════════════════════════════════════════════════════════════════════════
# ── PER-SYMBOL WORKER ──────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════
def analyse_symbol_live(fyers, symbol: str, market_ctx: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Fully live, stateless analysis of one symbol on confirmed 15-min
    candles. Returns (row_or_None, error_or_None). row is None with no
    error when there's simply no valid Order Block setup right now — that
    is the expected/normal case for most stocks on any given scan."""
    if not isinstance(symbol, str) or not _VALID_EQ_SYMBOL_RE.match(symbol):
        return None, f"{symbol}: invalid symbol format — skipped"

    df, err = fetch_confirmed_15m_candles(fyers, symbol)
    if err:
        return None, f"{symbol}: {err}"
    if df is None:
        return None, None  # insufficient history yet — not an error

    try:
        close = df["Close"]
        last_close = float(close.iloc[-1])
        last_candle_ist = df["Time"].iloc[-1].to_pydatetime()

        ema20 = float(close.ewm(span=20, adjust=False).mean().iloc[-1])
        ema50 = float(close.ewm(span=50, adjust=False).mean().iloc[-1])
        ema200 = float(close.ewm(span=200, adjust=False).mean().iloc[-1]) if len(close) >= 60 else None

        rsi_val = float(rsi(close).iloc[-1])
        macd_line, macd_signal, _ = macd(close)
        macd_bullish = bool(macd_line.iloc[-1] > macd_signal.iloc[-1])
        adx_val = float(adx(df).iloc[-1])
        atr_val = float(atr(df).iloc[-1])
        if pd.isna(atr_val) or atr_val <= 0:
            atr_val = last_close * 0.003
        supertrend_label, supertrend_bullish = supertrend(df)
        vwap_val = intraday_session_vwap(df)

        vol_avg20 = float(df["Volume"].tail(20).mean())
        last_volume = float(df["Volume"].iloc[-1])
        volume_ratio = round(last_volume / vol_avg20, 2) if vol_avg20 > 0 else 0.0

        structure = _market_structure(df)
        ob = detect_order_blocks_v2(df)

        if not ob["bullish_ob"] and not ob["bearish_ob"]:
            return None, None  # no Order Block right now — normal, not a failure

        direction = "BUY" if ob["bullish_ob"] else "SELL"
        is_buy = direction == "BUY"

        # ── BUY / SELL condition checklist (per spec) ──────────────────
        if is_buy:
            checklist = {
                "Fresh Bullish OB": ob["fresh"] or ob["breaker_block"],
                "RSI > 55": rsi_val > 55,
                "EMA20 > EMA50": ema20 > ema50,
                "MACD Bullish": macd_bullish,
                "Above VWAP": vwap_val is not None and last_close > vwap_val,
                "ADX > 25": adx_val > 25,
                "Volume > 20 SMA": volume_ratio > 1.0,
                "No Nearby Supply Zone": check_nearby_opposite_zone(direction, last_close, ob, atr_val) is None,
            }
        else:
            checklist = {
                "Fresh Bearish OB": ob["fresh"] or ob["breaker_block"],
                "RSI < 45": rsi_val < 45,
                "EMA20 < EMA50": ema20 < ema50,
                "MACD Bearish": not macd_bullish,
                "Below VWAP": vwap_val is not None and last_close < vwap_val,
                "ADX > 25": adx_val > 25,
                "Volume > 20 SMA": volume_ratio > 1.0,
                "No Nearby Demand Zone": check_nearby_opposite_zone(direction, last_close, ob, atr_val) is None,
            }

        confirmed_count = sum(checklist.values())
        conditions_met = [k for k, v in checklist.items() if v]
        conditions_failed = [k for k, v in checklist.items() if not v]

        # ── Entry / Stop Loss / Targets, anchored to the Order Block zone ─
        zone_low, zone_high = ob["zone_low"], ob["zone_high"]
        entry = round(last_close, 2)
        if is_buy:
            sl = round((zone_low - 0.25 * atr_val) if zone_low is not None else (entry - 1.2 * atr_val), 2)
            risk = max(entry - sl, 0.01)
            target1 = round(entry + 1.5 * risk, 2)
            target2 = round(entry + 3.0 * risk, 2)
        else:
            sl = round((zone_high + 0.25 * atr_val) if zone_high is not None else (entry + 1.2 * atr_val), 2)
            risk = max(sl - entry, 0.01)
            target1 = round(entry - 1.5 * risk, 2)
            target2 = round(entry - 3.0 * risk, 2)

        reward1 = abs(target1 - entry)
        rr_ratio = round(reward1 / risk, 2) if risk > 0 else 0.0

        # ── Live, stateless AI confidence (never reuses a past report) ──
        confidence, reasons = calculate_live_ai_confidence(
            direction=direction, rsi_val=rsi_val, ema20=ema20, ema50=ema50, ema200=ema200,
            macd_bullish=macd_bullish, vwap_val=vwap_val, last_close=last_close, adx_val=adx_val,
            volume_ratio=volume_ratio, ob=ob, rr_ratio=rr_ratio,
        )

        # ── Rejection filters ───────────────────────────────────────────
        avoid_reason = apply_ai_filters(
            direction=direction, volume_ratio=volume_ratio, adx_val=adx_val, rr_ratio=rr_ratio,
            ob=ob, last_candle_ist=last_candle_ist, is_market_holiday_or_closed=False,
        )
        if avoid_reason is None:
            avoid_reason = check_nearby_opposite_zone(direction, last_close, ob, atr_val)
        if avoid_reason is None:
            avoid_reason = apply_news_vix_filter(direction, market_ctx)

        # Confidence < 70% => ALWAYS WAIT, never BUY/SELL, no exceptions.
        if confidence < MIN_CONFIDENCE_FOR_TRADE:
            trade_decision = "🟡 WAIT"
            if avoid_reason is None:
                avoid_reason = f"Confidence {confidence:.1f}% is below the 70% minimum"
        elif avoid_reason is not None or confirmed_count < len(checklist):
            trade_decision = "🟡 WAIT"
        else:
            trade_decision = "🟢 BUY" if is_buy else "🔴 SELL"

        quality = _signal_quality_label(confidence, confirmed_count)
        stock_ticker = symbol.replace("NSE:", "").replace("-EQ", "")

        reason_text = (
            f"{direction} setup at a {ob['ob_type']} ({ob.get('zone_low')}–{ob.get('zone_high')}). "
            f"Confirms: {', '.join(conditions_met) if conditions_met else 'none'}."
        )
        if conditions_failed:
            reason_text += f" Not confirmed: {', '.join(conditions_failed)}."

        signal_date_str = last_candle_ist.astimezone(IST).strftime("%d-%b-%Y")
        signal_time_str = last_candle_ist.astimezone(IST).strftime("%H:%M:%S") + " IST"

        row: Dict[str, Any] = {
            "dedup_key": f"{symbol}|{signal_date_str}|{signal_time_str}|{direction}",
            "Signal Date": signal_date_str,
            "Signal Time": signal_time_str,
            "Stock": stock_ticker,
            "Symbol": symbol,
            "Order Block Type": ob["ob_type"],
            "Fresh/Mitigated": "Fresh" if ob["fresh"] else ("Mitigated" if ob["mitigated"] else "N/A"),
            "Breaker Block": "Yes" if ob["breaker_block"] else "No",
            "Demand Zone": ob["demand_zone"],
            "Supply Zone": ob["supply_zone"],
            "AI Confidence": confidence,
            "Entry": entry,
            "SL": sl,
            "Target 1": target1,
            "Target 2": target2,
            "Risk Reward": rr_ratio,
            "Volume Spike": f"{volume_ratio:.2f}x",
            "VWAP": vwap_val,
            "EMA Trend": "Bullish" if ema20 > ema50 else "Bearish",
            "RSI": round(rsi_val, 1),
            "MACD": "Bullish" if macd_bullish else "Bearish",
            "ADX": round(adx_val, 1),
            "ATR": round(atr_val, 2),
            "Supertrend": supertrend_label,
            "Liquidity": f"Demand {ob['demand_zone']} / Supply {ob['supply_zone']}",
            "Structure": structure,
            "Status": quality,
            "BUY/SELL": trade_decision,
            "Direction": direction,
            "Reason": reason_text,
            "Avoid Trade Reason": avoid_reason if avoid_reason else "—",
            "Confirmations": f"{confirmed_count}/{len(checklist)}",
            "Nifty Trend": market_ctx.get("nifty_trend", "Unknown"),
            "BankNifty Trend": market_ctx.get("banknifty_trend", "Unknown"),
            "India VIX": market_ctx.get("india_vix"),
        }
        return row, None
    except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError, AttributeError) as e:
        return None, f"{symbol}: analysis error ({type(e).__name__})"
    except Exception as e:  # pragma: no cover
        logger.exception("Unexpected error analysing %s", symbol)
        return None, f"{symbol}: unexpected error ({type(e).__name__})"


# ══════════════════════════════════════════════════════════════════════════
# ── DEDUP KEY PERSISTENCE ─────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════
def load_seen_keys() -> set:
    try:
        with open(_SEEN_V2_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data) if isinstance(data, list) else set()
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return set()


def save_seen_keys(keys: set) -> None:
    try:
        trimmed = sorted(keys)[-_SEEN_V2_MAX_KEEP:]
        with open(_SEEN_V2_FILE, "w", encoding="utf-8") as f:
            json.dump(trimmed, f)
    except OSError as e:
        logger.warning("Could not persist v2 dedup keys: %s", e)


# ══════════════════════════════════════════════════════════════════════════
# ── REPORT SAVING: CSV + JSON + TXT + PNG, filename SYMBOL_DATE_TIME_SIGNAL
# ══════════════════════════════════════════════════════════════════════════
def _safe_filename_base(row: Dict[str, Any]) -> str:
    safe_time = row["Signal Time"].replace(":", "").replace(" ", "_")
    signal_word = "BUY" if row["Direction"] == "BUY" else "SELL"
    return f"{row['Stock']}_{row['Signal Date']}_{safe_time}_{signal_word}"


def _write_txt(row: Dict[str, Any], folder: str, base_name: str) -> None:
    path = os.path.join(folder, f"{base_name}.txt")
    try:
        lines = [
            f"LIVE 15-MIN ORDER BLOCK AI SIGNAL — {row['BUY/SELL']}",
            "=" * 50,
            f"Stock              : {row['Stock']}",
            f"Signal Date/Time   : {row['Signal Date']} {row['Signal Time']}",
            f"Order Block Type   : {row['Order Block Type']}",
            f"Fresh/Mitigated    : {row['Fresh/Mitigated']}  |  Breaker Block: {row['Breaker Block']}",
            f"Demand Zone        : {row['Demand Zone']}   Supply Zone: {row['Supply Zone']}",
            f"AI Confidence      : {row['AI Confidence']}%",
            f"Entry              : {row['Entry']}",
            f"Stop Loss          : {row['SL']}",
            f"Target 1           : {row['Target 1']}",
            f"Target 2           : {row['Target 2']}",
            f"Risk:Reward        : {row['Risk Reward']}",
            f"Volume Spike       : {row['Volume Spike']}   VWAP: {row['VWAP']}",
            f"EMA Trend          : {row['EMA Trend']}   RSI: {row['RSI']}   MACD: {row['MACD']}",
            f"ADX                : {row['ADX']}   ATR: {row['ATR']}   Supertrend: {row['Supertrend']}",
            f"Structure          : {row['Structure']}",
            f"Confirmations      : {row['Confirmations']}",
            f"Nifty / BankNifty  : {row['Nifty Trend']} / {row['BankNifty Trend']}   India VIX: {row['India VIX']}",
            "-" * 50,
            f"Reason: {row['Reason']}",
            f"Avoid Trade Reason: {row['Avoid Trade Reason']}",
        ]
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError as e:
        logger.warning("Could not write TXT report: %s", e)


def _write_json(row: Dict[str, Any], folder: str, base_name: str) -> None:
    clean = {k: v for k, v in row.items() if k != "dedup_key"}
    path = os.path.join(folder, f"{base_name}.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(clean, f, indent=2, default=str)
    except OSError as e:
        logger.warning("Could not write JSON report: %s", e)

    try:
        history = []
        if os.path.exists(_V2_MASTER_JSON):
            with open(_V2_MASTER_JSON, "r", encoding="utf-8") as f:
                history = json.load(f)
        history.append(clean)
        with open(_V2_MASTER_JSON, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2, default=str)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Could not append master JSON log: %s", e)


def _write_csv(row: Dict[str, Any], folder: str, base_name: str) -> None:
    clean = {k: v for k, v in row.items() if k != "dedup_key"}
    fieldnames = list(clean.keys())
    path = os.path.join(folder, f"{base_name}.csv")
    try:
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerow(clean)
    except OSError as e:
        logger.warning("Could not write CSV report: %s", e)

    try:
        exists = os.path.exists(_V2_MASTER_CSV)
        with open(_V2_MASTER_CSV, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if not exists:
                writer.writeheader()
            writer.writerow(clean)
    except OSError as e:
        logger.warning("Could not append master CSV log: %s", e)


def _write_chart(df: pd.DataFrame, row: Dict[str, Any], folder: str, base_name: str) -> None:
    if not MATPLOTLIB_AVAILABLE:
        return
    path = os.path.join(folder, f"{base_name}.png")
    try:
        plot_df = df.tail(60).reset_index(drop=True)
        fig, ax_ = plt.subplots(figsize=(11, 6))
        for i, candle in plot_df.iterrows():
            color = "#26a69a" if candle["Close"] >= candle["Open"] else "#ef5350"
            ax_.plot([i, i], [candle["Low"], candle["High"]], color=color, linewidth=1)
            ax_.add_patch(plt.Rectangle(
                (i - 0.3, min(candle["Open"], candle["Close"])),
                0.6, max(abs(candle["Close"] - candle["Open"]), 1e-6),
                facecolor=color, edgecolor=color,
            ))
        ax_.axhline(row["Entry"], color="blue", linestyle="--", linewidth=1.2, label=f"Entry {row['Entry']}")
        ax_.axhline(row["SL"], color="red", linestyle="--", linewidth=1.2, label=f"SL {row['SL']}")
        ax_.axhline(row["Target 1"], color="green", linestyle="--", linewidth=1.2, label=f"T1 {row['Target 1']}")
        ax_.axhline(row["Target 2"], color="darkgreen", linestyle=":", linewidth=1.2, label=f"T2 {row['Target 2']}")
        ax_.set_title(f"{row['Stock']} — {row['BUY/SELL']} @ {row['Signal Date']} {row['Signal Time']}")
        ax_.set_xlabel("15-Min Candle #")
        ax_.set_ylabel("Price")
        ax_.legend(loc="best", fontsize=8)
        fig.tight_layout()
        fig.savefig(path, dpi=120)
        plt.close(fig)
    except Exception as e:  # pragma: no cover
        logger.warning("Could not save chart: %s", e)
        try:
            plt.close("all")
        except Exception:
            pass


def persist_signal(df: pd.DataFrame, row: Dict[str, Any]) -> None:
    """Saves the signal into BOTH its directional folder (buy/sell) and the
    permanent history/orderblock audit folders, in all four formats."""
    ensure_v2_folders()
    base_name = _safe_filename_base(row)
    is_buy = row["Direction"] == "BUY"

    signal_folder = V2_SIGNALS_BUY_DIR if is_buy else V2_SIGNALS_SELL_DIR
    chart_folder = V2_CHARTS_BUY_DIR if is_buy else V2_CHARTS_SELL_DIR

    for folder in (signal_folder, V2_SIGNALS_HISTORY_DIR, V2_SIGNALS_ORDERBLOCK_DIR):
        _write_txt(row, folder, base_name)
        _write_json(row, folder, base_name)
        _write_csv(row, folder, base_name)

    _write_chart(df, row, chart_folder, base_name)
    _write_chart(df, row, V2_CHARTS_HISTORY_DIR, base_name)

    logger.info(
        "New live OB signal: %s %s @ %s %s (Entry=%s SL=%s T1=%s T2=%s Conf=%s%%)",
        row["Stock"], row["Direction"], row["Signal Date"], row["Signal Time"],
        row["Entry"], row["SL"], row["Target 1"], row["Target 2"], row["AI Confidence"],
    )


# ══════════════════════════════════════════════════════════════════════════
# ── THREADED LIVE SCAN ─────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════
def run_live_scan(fyers, symbols: List[str], seen_keys: set, progress_cb=None):
    """Runs one full live scan cycle across `symbols`. Every call re-fetches
    everything from the live API — nothing from a previous cycle is reused
    except the dedup key set (which only prevents duplicate SAVES, not
    duplicate scoring/recalculation).

    Returns: (all_rows, new_rows, errors, stats_dict, updated_seen_keys)
    """
    symbols = _validate_symbols(symbols)
    market_ctx = fetch_market_context(fyers)

    all_rows: List[dict] = []
    new_rows: List[dict] = []
    errors: List[str] = []
    updated_keys = set(seen_keys)

    total = len(symbols)
    scanned = 0
    signals_found = 0
    start = time.time()

    for i in range(0, total, BATCH_SIZE):
        batch = symbols[i:i + BATCH_SIZE]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(analyse_symbol_live, fyers, s, market_ctx): s for s in batch}
            for future in as_completed(futures):
                try:
                    res, err = future.result()
                except Exception as e:
                    res, err = None, f"{futures[future]}: worker error ({type(e).__name__})"

                if res:
                    all_rows.append(res)
                    signals_found += 1
                    if res["dedup_key"] not in updated_keys:
                        updated_keys.add(res["dedup_key"])
                        new_rows.append(res)
                if err:
                    errors.append(err)

                scanned += 1
                if progress_cb:
                    progress_cb(scanned, total, signals_found, len(errors))

        if i + BATCH_SIZE < total:
            time.sleep(BATCH_PAUSE_SECONDS)

    stats = {
        "total": total, "scanned": scanned, "signals_found": signals_found,
        "errors": len(errors), "elapsed_seconds": round(time.time() - start, 1),
        "market_context": market_ctx,
    }
    save_seen_keys(updated_keys)
    return all_rows, new_rows, errors, stats, updated_keys


def persist_new_signals(fyers, new_rows: List[dict]) -> None:
    """Re-fetches the confirmed candles once more for each brand-new signal
    (cheap — only the handful of genuinely new rows per cycle) purely to
    have OHLCV data for the chart, then saves TXT/CSV/JSON/PNG."""
    for row in new_rows:
        try:
            df, err = fetch_confirmed_15m_candles(fyers, row["Symbol"])
            if err or df is None:
                continue
            persist_signal(df, row)
        except Exception as e:  # pragma: no cover
            logger.warning("Could not persist new signal for %s: %s", row.get("Stock"), e)


# ══════════════════════════════════════════════════════════════════════════
# ── EXPORT HELPERS (CSV/JSON/Excel bytes for Streamlit download buttons) ──
# ══════════════════════════════════════════════════════════════════════════
def rows_to_csv_bytes(rows: List[dict]) -> bytes:
    df = pd.DataFrame([{k: v for k, v in r.items() if k not in ("dedup_key", "Symbol")} for r in rows])
    return df.to_csv(index=False).encode("utf-8")


def rows_to_json_bytes(rows: List[dict]) -> bytes:
    df = pd.DataFrame([{k: v for k, v in r.items() if k not in ("dedup_key", "Symbol")} for r in rows])
    return df.to_json(orient="records", indent=2, force_ascii=False).encode("utf-8")


def rows_to_excel_bytes(rows: List[dict], sheet_name: str = "Live OB AI Signals") -> bytes:
    df = pd.DataFrame([{k: v for k, v in r.items() if k not in ("dedup_key", "Symbol")} for r in rows])
    import io as _io
    buf = _io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name=sheet_name[:31])
    buf.seek(0)
    return buf.getvalue()


def _style_row(val):
    if isinstance(val, str):
        if "BUY" in val or "🟢" in val:
            return "color: green; font-weight: bold;"
        if "SELL" in val or "🔴" in val:
            return "color: red; font-weight: bold;"
        if "WAIT" in val or "🟡" in val:
            return "color: #b8860b; font-weight: bold;"
    return ""


def _style_df(df: pd.DataFrame):
    styler = df.style
    if hasattr(styler, "map"):
        return styler.map(_style_row)
    return styler.applymap(_style_row)


# ══════════════════════════════════════════════════════════════════════════
# ── STREAMLIT TAB RENDERER ─────────────────────────────────────────────────
# Call this from inside a `with tab:` block in your existing show_scanner().
# It does not read or write any of your existing session_state keys, so it
# cannot collide with or break any existing scanner tab.
# ══════════════════════════════════════════════════════════════════════════
def render_live_ob_ai_tab(fyers, all_symbols: List[str]) -> None:
    st.subheader("🎯 Live 15-Min Order Block AI Scanner (True Engine)")
    st.caption(
        "Every number on this tab is recomputed live on each scan from the last FULLY "
        "CLOSED 15-minute candle — nothing is cached or reused between refreshes, and "
        "confidence below 70% always shows WAIT, never BUY/SELL."
    )

    col1, col2 = st.columns([2, 1])
    with col1:
        default_watchlist = all_symbols[: min(20, len(all_symbols))]
        watchlist = st.multiselect(
            "F&O / NSE stocks to monitor", options=all_symbols, default=default_watchlist,
            key="v2_ob_watchlist",
            help="Keep this under ~40 symbols when auto-refresh is on — a fresh 15-min "
                 "history call is made per symbol every 30 seconds.",
        )
    with col2:
        auto_refresh = st.checkbox(
            f"🔁 Auto-refresh every {AUTO_REFRESH_SECONDS}s", value=False, key="v2_ob_auto_refresh"
        )

    run_now = st.button(f"🔎 Run Live Scan ({len(watchlist)} symbols)", key="v2_ob_run")

    if run_now or auto_refresh:
        if not watchlist:
            st.warning("Select at least one stock to monitor above.")
        else:
            seen_keys = load_seen_keys()
            progress_bar = st.progress(0.0, text="Scanning 0 / 0")

            def _cb(scanned, total, found, errs):
                progress_bar.progress(
                    scanned / max(total, 1),
                    text=f"Scanning {scanned} / {total} — Signals: {found} — Errors: {errs}",
                )

            with st.spinner("Fetching live market data and recalculating…"):
                all_rows, new_rows, errors, stats, updated_keys = run_live_scan(
                    fyers, watchlist, seen_keys, progress_cb=_cb
                )
                if new_rows:
                    persist_new_signals(fyers, new_rows)
            progress_bar.empty()

            st.session_state["v2_ob_rows"] = all_rows
            st.session_state["v2_ob_errors"] = errors
            st.session_state["v2_ob_stats"] = stats
            st.session_state["v2_ob_last_scan"] = _now_ist()

            for nr in new_rows:
                msg = (
                    f"{nr['BUY/SELL']} {nr['Stock']} @ {nr['Entry']} (SL {nr['SL']}, "
                    f"T1 {nr['Target 1']}, T2 {nr['Target 2']}, RR {nr['Risk Reward']}, "
                    f"Confidence {nr['AI Confidence']}%) — {nr['Signal Date']} {nr['Signal Time']}"
                )
                if nr["Direction"] == "BUY" and "BUY" in nr["BUY/SELL"]:
                    st.success(f"🟢 NEW BUY: {msg}")
                elif nr["Direction"] == "SELL" and "SELL" in nr["BUY/SELL"]:
                    st.error(f"🔴 NEW SELL: {msg}")
                else:
                    st.info(f"🟡 NEW WATCH: {msg}")
                try:
                    st.toast(msg, icon="🔔")
                except Exception:
                    pass

    # ── Live status strip ──────────────────────────────────────────────
    stats = st.session_state.get("v2_ob_stats")
    last_scan = st.session_state.get("v2_ob_last_scan")
    if stats:
        s1, s2, s3, s4, s5 = st.columns(5)
        s1.metric("Stocks Scanned", f"{stats['scanned']}/{stats['total']}")
        s2.metric("Signals Found", stats["signals_found"])
        s3.metric("Errors", stats["errors"])
        s4.metric("Scan Time", f"{stats['elapsed_seconds']}s")
        if last_scan:
            next_scan = last_scan + timedelta(seconds=AUTO_REFRESH_SECONDS)
            s5.metric("Next Scan", next_scan.strftime("%H:%M:%S") if auto_refresh else "Manual")
        mctx = stats.get("market_context", {})
        st.caption(
            f"Market context (live): Nifty {mctx.get('nifty_trend', 'Unknown')} · "
            f"BankNifty {mctx.get('banknifty_trend', 'Unknown')} · "
            f"India VIX {mctx.get('india_vix', 'N/A')} ({mctx.get('vix_regime', 'Unknown')}) · "
            f"Gap: {mctx.get('gap_regime', 'Unknown')}"
        )
    if last_scan:
        st.caption(f"Last scan: {last_scan.strftime('%d-%b-%Y %H:%M:%S')} IST")

    # ── Dashboard table ─────────────────────────────────────────────────
    rows = st.session_state.get("v2_ob_rows")
    if rows:
        display_cols = [
            "Signal Date", "Signal Time", "Stock", "Order Block Type", "Fresh/Mitigated",
            "Breaker Block", "AI Confidence", "Entry", "SL", "Target 1", "Target 2",
            "Risk Reward", "Volume Spike", "VWAP", "EMA Trend", "RSI", "MACD", "ADX",
            "ATR", "Supertrend", "Liquidity", "Structure", "Status", "BUY/SELL",
            "Avoid Trade Reason",
        ]
        df_display = pd.DataFrame(rows)[[c for c in display_cols if c in pd.DataFrame(rows).columns]]
        df_sorted = df_display.sort_values("AI Confidence", ascending=False)
        st.dataframe(_style_df(df_sorted), use_container_width=True, height=480)

        d1, d2, d3 = st.columns(3)
        with d1:
            st.download_button(
                "📥 Download Excel", data=rows_to_excel_bytes(rows),
                file_name=f"live_ob_ai_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key="v2_dl_xlsx",
            )
        with d2:
            st.download_button(
                "📥 Download CSV", data=rows_to_csv_bytes(rows),
                file_name=f"live_ob_ai_{_now_ist().strftime('%Y%m%d_%H%M')}.csv",
                mime="text/csv", key="v2_dl_csv",
            )
        with d3:
            st.download_button(
                "📥 Download JSON", data=rows_to_json_bytes(rows),
                file_name=f"live_ob_ai_{_now_ist().strftime('%Y%m%d_%H%M')}.json",
                mime="application/json", key="v2_dl_json",
            )

        if os.path.exists(_V2_MASTER_CSV):
            with open(_V2_MASTER_CSV, "rb") as f:
                st.download_button(
                    "📥 Download All-Time Signal History (CSV)", data=f.read(),
                    file_name="live_ob_ai_all_time_history.csv", mime="text/csv",
                    key="v2_dl_history_csv",
                )
    else:
        st.info("Run a live scan above (or enable auto-refresh) to see confirmed signals here.")

    errors = st.session_state.get("v2_ob_errors")
    if errors:
        with st.expander(f"⚠️ Skipped/failed symbols ({len(errors)})"):
            st.caption("Most stocks are simply skipped for missing/invalid data, not app errors.")
            st.text("\n".join(errors[:25]))

    if auto_refresh:
        time.sleep(AUTO_REFRESH_SECONDS)
        st.rerun()


# ══════════════════════════════════════════════════════════════════════════
# ── INTEGRATION SNIPPET (copy into scaner.py — do not paste this comment) ──
# ══════════════════════════════════════════════════════════════════════════
#
# 1) Near your other imports at the top of scaner.py, add:
#
#        from live_ob_ai_engine import render_live_ob_ai_tab
#
# 2) Inside show_scanner(), find the st.tabs([...]) call that lists all
#    your existing tabs, and add ONE more label to the list, e.g.:
#
#        tab_scanner, tab_intraday, tab_swing, tab_fo, \
#            tab_intraday_cisd, tab_fo_cisd, tab_golden_death, tab_premarket, tab_fo_15m_cisd, \
#            tab_live_ob, tab_ema_swing, tab_live_ob_ai_v2 = st.tabs(
#            ["📊 Full Scanner", "⚡ Intraday Scanner", "📈 Swing Trade Scanner", "🏛️ F&O Stocks Scanner",
#             "🕐 Intraday CISD Signals", "🎯 F&O CISD Scanner", "✝️ Swing Trading (Golden/Death Cross)",
#             "🌅 Pre-Market Scanner", "🎯 NSE F&O 15-Min CISD Scanner", "🔔 Live OB Signal Scanner",
#             "🌟 EMA 50/200 Swing (4H)", "🎯 Live OB AI Scanner v2"]
#        )
#
# 3) Add a new `with` block anywhere after the existing tabs (does not
#    touch any of them):
#
#        with tab_live_ob_ai_v2:
#            fo_symbols_v2 = load_nse_fo_stock_symbols()
#            render_live_ob_ai_tab(fyers, fo_symbols_v2 if fo_symbols_v2 else symbols)
#
# That's it — every existing tab, function, folder, and file in scaner.py
# is completely untouched.
# ══════════════════════════════════════════════════════════════════════════
