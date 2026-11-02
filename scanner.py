import streamlit as st
import pandas as pd
import numpy as np
import requests
import time
import io
import os
import re
import html
import json
import csv
import gc
import logging
import sys
import threading
from datetime import datetime, timedelta
from typing import List, Optional, Tuple, Dict, Any
from concurrent.futures import ThreadPoolExecutor, as_completed

# Optional clipboard-image support for the AI Chart Analysis tab.
try:
    from streamlit_paste_button import paste_image_button
    PASTE_IMAGE_AVAILABLE = True
except Exception:
    paste_image_button = None
    PASTE_IMAGE_AVAILABLE = False


# ============================================================
# INDEPENDENT STRONG SIGNALS / MARKET DASHBOARD HELPERS
# ============================================================

def _show_strong_signals_button():
    """Independent Strong Signals runner."""
    if st.button("🔥 RUN STRONG SIGNALS", key="run_strong_signals", use_container_width=True):
        source = st.session_state.get("strong_source", "NSE Stocks")
        df = st.session_state.get("nse_df" if source == "NSE Stocks" else "fo_df")

        if df is None or len(df) == 0:
            st.warning(f"⚠️ Run the {source} scanner first.")
            return

        confidence_col = next(
            (c for c in ["AI CONFIDENCE %", "CONFIDENCE %", "Confidence %"]
             if c in df.columns),
            None
        )
        signal_col = next(
            (c for c in ["AI SIGNAL", "SIGNAL", "Signal"]
             if c in df.columns),
            None
        )

        if confidence_col is None:
            st.error("❌ Confidence column not found in scanner output.")
            return

        out = df.copy()
        conf = pd.to_numeric(out[confidence_col], errors="coerce").fillna(0)
        out = out[conf >= 75].copy()

        if signal_col:
            out = out[
                ~out[signal_col].astype(str).str.upper().isin(
                    ["NEUTRAL", "NO SIGNAL", "NONE", "N/A"]
                )
            ]

        if len(out) == 0:
            st.warning("No strong signals ≥75% found.")
        else:
            out = out.sort_values(confidence_col, ascending=False)
            st.success(f"🔥 {len(out)} strong signals found.")
            st.dataframe(out, use_container_width=True, height=450)


def _show_market_dashboard_button():
    """Independent Market Dashboard runner."""
    if st.button("📊 RUN MARKET DASHBOARD", key="run_market_dashboard", use_container_width=True):
        source = st.session_state.get("dash_source", "NSE Stocks")
        df = st.session_state.get("nse_df" if source == "NSE Stocks" else "fo_df")

        if df is None or len(df) == 0:
            st.warning(f"⚠️ Run the {source} scanner first.")
            return

        signal_col = next(
            (c for c in ["AI SIGNAL", "SIGNAL", "Signal"]
             if c in df.columns),
            None
        )
        confidence_col = next(
            (c for c in ["AI CONFIDENCE %", "CONFIDENCE %", "Confidence %"]
             if c in df.columns),
            None
        )

        if signal_col is None:
            st.error("❌ Signal column not found.")
            return

        s = df[signal_col].astype(str).str.upper()

        buy = int(s.str.contains("BUY", na=False).sum())
        sell = int(s.str.contains("SELL", na=False).sum())
        neutral = int(s.isin(["NEUTRAL", "NONE", "NO SIGNAL", "N/A"]).sum())

        strong_buy = 0
        strong_sell = 0
        avg_conf = 0.0

        if confidence_col:
            conf = pd.to_numeric(
                df[confidence_col], errors="coerce"
            ).fillna(0)

            strong_buy = int(
                ((s.str.contains("BUY", na=False)) & (conf >= 75)).sum()
            )
            strong_sell = int(
                ((s.str.contains("SELL", na=False)) & (conf >= 75)).sum()
            )
            avg_conf = float(conf.mean()) if len(conf) else 0.0

        total = len(df)

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("TOTAL", total)
        c2.metric("🟢 BUY", buy)
        c3.metric("🔴 SELL", sell)
        c4.metric("🔥 STRONG BUY", strong_buy)
        c5.metric("🔥 STRONG SELL", strong_sell)

        st.metric("Average Confidence", f"{avg_conf:.1f}%")

        if buy > sell * 1.15:
            sentiment = "🟢 BULLISH"
        elif sell > buy * 1.15:
            sentiment = "🔴 BEARISH"
        else:
            sentiment = "🟡 NEUTRAL"

        st.subheader(f"Market Sentiment: {sentiment}")

        dash = pd.DataFrame({
            "Metric": [
                "Total Stocks",
                "BUY",
                "SELL",
                "NEUTRAL",
                "Strong BUY",
                "Strong SELL",
                "Average Confidence"
            ],
            "Value": [
                total,
                buy,
                sell,
                neutral,
                strong_buy,
                strong_sell,
                f"{avg_conf:.1f}%"
            ]
        })
        st.dataframe(dash, use_container_width=True, hide_index=True)




def _excel_download_button(df: pd.DataFrame, filename_prefix: str, key: str, label: str = "📥 DOWNLOAD EXCEL"):
    """Render an Excel download button for a DataFrame."""
    if df is None or df.empty:
        return
    try:
        excel_data = _format_excel_output(df, filename_prefix)
        filename = f"{filename_prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        st.download_button(
            label,
            data=excel_data,
            file_name=filename,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=key,
            use_container_width=True,
        )
    except Exception as e:
        st.error(f"❌ Excel export failed: {str(e)[:150]}")

# ════════════════════════════════════════════════════════════════════════════════
# IMPORTS & CONFIG (FROM ORIGINAL)
# ════════════════════════════════════════════════════════════════════════════════
try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False

try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
except Exception:
    from datetime import timezone
    IST = timezone(timedelta(hours=5, minutes=30))

try:
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    OPENPYXL_AVAILABLE = True
except ImportError:
    OPENPYXL_AVAILABLE = False

# ════════════════════════════════════════════════════════════════════════════════
# CONSTANTS (ORIGINAL + NEW)
# ════════════════════════════════════════════════════════════════════════════════
DATE_FROM = (datetime.today() - timedelta(days=365)).strftime("%Y-%m-%d")
DATE_TO = datetime.today().strftime("%Y-%m-%d")
FYERS_NSE_CM_SYMBOL_MASTER = "https://public.fyers.in/sym_details/NSE_CM.csv"
NIFTY_BENCHMARK_SYMBOL = "NSE:NIFTY50-INDEX"
MAX_WORKERS = 8
BATCH_SIZE = 50
BATCH_PAUSE_SECONDS = 1.0
DEFAULT_SCAN_STOCKS = 2300
FYERS_APP_ID = os.environ.get("FYERS_APP_ID", "")
OPTIONS_STRIKE_COUNT = 10
OPTIONS_HTTP_TIMEOUT = 15

# ════════════════════════════════════════════════════════════════════════════════
# V17 NEW CONSTANTS
# ════════════════════════════════════════════════════════════════════════════════
DEFAULT_CONFIDENCE_THRESHOLD = 70
DEFAULT_RVOL_THRESHOLD = 1.2
DEFAULT_STRONG_RVOL = 1.5
GOLDEN_CROSS_MIN_SIGNALS = 1
DEATH_CROSS_MIN_SIGNALS = 1
ADDITIONAL_ANALYSIS_LOOKBACK_DAYS = 30
REFRESH_INTERVALS = [30, 60, 120]

# ════════════════════════════════════════════════════════════════════════════════
# MOMENTUM MOVERS CONSTANTS (NEW)
# ════════════════════════════════════════════════════════════════════════════════
MOMENTUM_MIN_SCORE = 65
MOMENTUM_STRONG_SCORE = 85
MOMENTUM_DEVELOPING_SCORE = 55
# LIVE movement scanner: recent 5M price action only. No consolidation requirement.
LIVE_MOVE_MIN_PCT = 0.35
LIVE_MOVE_BIG_PCT = 0.70
LIVE_MOVE_MIN_RVOL = 1.30
LIVE_MOVE_STRONG_RVOL = 1.80
LIVE_MOVE_MIN_BODY_PCT = 50.0
LIVE_MOVE_MIN_SCORE = 65
LIVE_MOVE_STRONG_SCORE = 85
LIVE_MOVE_LOOKBACK_DAYS = 2
MOMENTUM_MIN_MOVE_PCT = 0.10
MOMENTUM_MIN_RVOL = 1.20
MOMENTUM_MIN_BODY_PCT = 20
MOMENTUM_DISPLAY_COUNT = 10

# ════════════════════════════════════════════════════════════════════════════════
# LIVE EXCHANGE MARKET DEPTH / ORDER FLOW
# Uses FYERS Market Depth snapshots (real bid/ask book), not TradingView footprint.
# ════════════════════════════════════════════════════════════════════════════════
DEPTH_SCAN_LIMIT_DEFAULT = 30
DEPTH_MIN_IMBALANCE = 15.0
DEPTH_STRONG_IMBALANCE = 35.0
DEPTH_BIG_QTY_MULTIPLIER = 2.0
DEPTH_TOP_LEVELS = 5

# ════════════════════════════════════════════════════════════════════════════════
# 15-MIN REVERSAL SCANNER CONSTANTS (ORIGINAL)
# ════════════════════════════════════════════════════════════════════════════════
REVERSAL_RESOLUTION = "15"
REVERSAL_LOOKBACK_DAYS = 5
REVERSAL_CONFIRMATION_BARS = 0
REVERSAL_ATR_LENGTH = 5
REVERSAL_ATR_MULTIPLIER = 2.8
REVERSAL_MIN_MOVE_PCT = 0.015
REVERSAL_CUSTOM_ABS = 0.05

# ════════════════════════════════════════════════════════════════════════════════
# VOLUME BIG MOVEMENT CONSTANTS (ORIGINAL)
# ════════════════════════════════════════════════════════════════════════════════
VOL_BIGMOVE_MIN_RVOL = 2.5
VOL_BIGMOVE_MIN_BODY_PCT = 0.8
VOL_BIGMOVE_LOOKBACK = 20

# BIG MOVE detector thresholds (required by detect_last_consolidation / detect_big_move_setup).
# Kept separate from the original VOL_BIGMOVE_* settings to avoid changing old logic.
BIGMOVE_LOOKBACK_BARS = 20
BIGMOVE_CONSOLIDATION_MIN_BARS = 4
BIGMOVE_CONSOLIDATION_MAX_BARS = 12
BIGMOVE_MAX_RANGE_PCT = 1.8
BIGMOVE_MAX_BAR_ATR_MULT = 1.25
BIGMOVE_MIN_BODY_ATR = 0.60
BIGMOVE_MIN_BODY_PCT = 55.0
BIGMOVE_MIN_RVOL = 1.50
BIGMOVE_MIN_BREAK_PCT = 0.05
BIGMOVE_STRONG_SCORE = 80.0

# ════════════════════════════════════════════════════════════════════════════════
# MULTI-TIMEFRAME CONSTANTS (NEW)
# ════════════════════════════════════════════════════════════════════════════════
SWING_LOOKBACK_PERIODS = 20
EMA_PERIODS = [9, 21, 50, 200]
VWAP_LOOKBACK = 20
MSS_MIN_CONFIRMATION = 1
STRUCTURE_TIMEFRAMES = ["5", "15", "60"]
MASTER_SIGNAL_LOOKBACK_DAYS = 10

# ════════════════════════════════════════════════════════════════════════════════
# LOGGING & UTILITIES (ORIGINAL)
# ════════════════════════════════════════════════════════════════════════════════
def _now_ist() -> datetime:
    return datetime.now(IST)

def _ensure_app_folders() -> None:
    for folder in ("logs", "charts", "exports"):
        os.makedirs(folder, exist_ok=True)

_ensure_app_folders()
logger = logging.getLogger("nse_scanner_v17")
logger.setLevel(logging.DEBUG)

if not logger.handlers:
    fh = logging.FileHandler("logs/scanner.log", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    fh.setFormatter(formatter)
    logger.addHandler(fh)

def _candle_signal_timestamp(df, is_daily: bool = False, resolution: str = "15") -> Tuple[str, str]:
    ts = df["Time"].iloc[-1]
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    ts_ist = ts.tz_convert(IST)
    if is_daily:
        close_ts = ts_ist.replace(hour=15, minute=30, second=0, microsecond=0)
    else:
        try:
            minutes = int(resolution)
        except Exception:
            minutes = 15
        close_ts = ts_ist + timedelta(minutes=minutes)
    return close_ts.strftime("%d-%b-%Y"), close_ts.strftime("%H:%M:%S") + " IST"

def _generated_timestamp() -> str:
    return _now_ist().strftime("%d-%b-%Y %H:%M:%S IST")

# ════════════════════════════════════════════════════════════════════════════════
# CORE INDICATORS (RETAINED FROM ORIGINAL)
# ════════════════════════════════════════════════════════════════════════════════
def calculate_rsi(close, period: int = 14):
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return (100 - (100 / (1 + rs))).fillna(50)

def calculate_macd(close):
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    return macd_line, signal_line, macd_line - signal_line

def calculate_atr(df, period: int = 14):
    h, l, c = df["High"], df["Low"], df["Close"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

def _last_valid_atr(df, period: int = 14) -> float:
    atr_series = calculate_atr(df, period)
    val = atr_series.iloc[-1] if len(atr_series) else np.nan
    if pd.isna(val) or val <= 0:
        last_close = float(df["Close"].iloc[-1]) if len(df) else 0.0
        val = max(last_close * 0.005, 0.01)
    return float(val)

# ════════════════════════════════════════════════════════════════════════════════
# BUYING/SELLING PRESSURE INDICATORS
# ════════════════════════════════════════════════════════════════════════════════
def calculate_buying_selling_pressure(df) -> Dict[str, Any]:
    if len(df) < 5:
        return {
            "buying_pressure": 50, "selling_pressure": 50,
            "buying_volume": 0, "selling_volume": 0,
            "pressure_ratio": 1.0, "trend": "NEUTRAL"
        }
    
    recent = df.tail(20).copy()
    buying_vol = 0.0
    selling_vol = 0.0
    
    for idx in range(len(recent)):
        candle = recent.iloc[idx]
        close = float(candle["Close"])
        open_ = float(candle["Open"])
        volume = float(candle["Volume"])
        
        if close > open_:
            buying_vol += volume
        elif close < open_:
            selling_vol += volume
        else:
            buying_vol += volume * 0.5
            selling_vol += volume * 0.5
    
    total_vol = buying_vol + selling_vol
    if total_vol == 0:
        return {
            "buying_pressure": 50, "selling_pressure": 50,
            "buying_volume": 0, "selling_volume": 0,
            "pressure_ratio": 1.0, "trend": "NEUTRAL"
        }
    
    bp_pct = (buying_vol / total_vol) * 100
    sp_pct = (selling_vol / total_vol) * 100
    pressure_ratio = buying_vol / selling_vol if selling_vol > 0 else float('inf')
    
    if bp_pct > 65:
        trend = "STRONG_BUYING"
    elif bp_pct > 55:
        trend = "BUYING"
    elif sp_pct > 65:
        trend = "STRONG_SELLING"
    elif sp_pct > 55:
        trend = "SELLING"
    else:
        trend = "NEUTRAL"
    
    return {
        "buying_pressure": round(bp_pct, 1),
        "selling_pressure": round(sp_pct, 1),
        "buying_volume": round(buying_vol, 0),
        "selling_volume": round(selling_vol, 0),
        "pressure_ratio": round(pressure_ratio, 2) if pressure_ratio != float('inf') else 0,
        "trend": trend
    }

# ════════════════════════════════════════════════════════════════════════════════
# ESTIMATED ORDER-FLOW DELTA (OHLCV PROXY)
# ════════════════════════════════════════════════════════════════════════════════
def calculate_order_flow_delta(df: pd.DataFrame, lookback: int = 20) -> Dict[str, Any]:
    """Estimate buy/sell volume using each candle's close location in its range.

    IMPORTANT: OHLCV candles do not identify the aggressor for each trade.
    These are estimated buy/sell volumes and delta, NOT true exchange trade delta.
    A true delta requires trade-by-trade prints with aggressor-side classification.
    """
    empty = {
        "estimated_buy_volume": 0.0, "estimated_sell_volume": 0.0,
        "volume_delta": 0.0, "delta_pct": 0.0, "cumulative_delta": 0.0,
        "order_flow_signal": "WAIT", "delta_divergence": "NONE",
        "last_candle_delta": 0.0,
    }
    required = ["Open", "High", "Low", "Close", "Volume"]
    if df is None or len(df) < 2 or any(c not in df.columns for c in required):
        return empty
    try:
        d = df.tail(max(2, int(lookback))).copy()
        for col in required:
            d[col] = pd.to_numeric(d[col], errors="coerce")
        d = d.dropna(subset=required)
        if d.empty:
            return empty
        candle_range = (d["High"] - d["Low"]).clip(lower=0)
        # Close Location Value: 0 at candle low, 1 at candle high.
        clv = ((d["Close"] - d["Low"]) / candle_range.replace(0, np.nan)).clip(0, 1).fillna(0.5)
        volume = d["Volume"].clip(lower=0)
        buy_vol = volume * clv
        sell_vol = volume - buy_vol
        delta_series = buy_vol - sell_vol
        total_volume = float(volume.sum())
        delta = float(delta_series.sum())
        delta_pct = (delta / total_volume * 100.0) if total_volume > 0 else 0.0
        buy_total = float(buy_vol.sum())
        sell_total = float(sell_vol.sum())
        last_delta = float(delta_series.iloc[-1])
        if delta_pct >= 20:
            signal = "STRONG BUYING"
        elif delta_pct >= 7:
            signal = "BUYING"
        elif delta_pct <= -20:
            signal = "STRONG SELLING"
        elif delta_pct <= -7:
            signal = "SELLING"
        else:
            signal = "BALANCED"
        price_change = float(d["Close"].iloc[-1] - d["Close"].iloc[0])
        if price_change > 0 and delta < 0:
            divergence = "BEARISH DIVERGENCE"
        elif price_change < 0 and delta > 0:
            divergence = "BULLISH DIVERGENCE"
        else:
            divergence = "NONE"
        return {
            "estimated_buy_volume": round(buy_total, 0),
            "estimated_sell_volume": round(sell_total, 0),
            "volume_delta": round(delta, 0),
            "delta_pct": round(delta_pct, 1),
            "cumulative_delta": round(float(delta_series.cumsum().iloc[-1]), 0),
            "order_flow_signal": signal,
            "delta_divergence": divergence,
            "last_candle_delta": round(last_delta, 0),
        }
    except Exception as exc:
        logger.debug("Order-flow delta calculation failed: %s", exc)
        return empty


# ════════════════════════════════════════════════════════════════════════════════
# MOMENTUM SCORING ENGINE (NEW - V17)
# ════════════════════════════════════════════════════════════════════════════════
def calculate_movement_metrics(df) -> Dict[str, float]:
    """Measure the latest CLOSED candle and short-term movement."""
    empty = {
        "move_5m_pct": 0.0,
        "move_15m_pct": 0.0,
        "candle_body_pct": 0.0,
        "atr_normalized_move": 0.0,
        "price_acceleration": 0.0,
    }
    if df is None or len(df) < 10:
        return empty
    try:
        d = df.reset_index(drop=True).copy()
        last = d.iloc[-1]
        prev = d.iloc[-2]
        close = float(last["Close"])
        open_ = float(last["Open"])
        high = float(last["High"])
        low = float(last["Low"])
        prev_close = float(prev["Close"])
        base_15 = float(d["Close"].iloc[-4]) if len(d) >= 4 else prev_close
        rng = max(high - low, 0.0)
        body = abs(close - open_)
        atr_series = calculate_atr(d, 14)
        atr = float(atr_series.iloc[-1]) if len(atr_series) and pd.notna(atr_series.iloc[-1]) else max(close * 0.005, 0.01)
        prev_body = abs(float(prev["Close"]) - float(prev["Open"]))
        return {
            "move_5m_pct": round(abs(close-prev_close)/prev_close*100, 2) if prev_close else 0.0,
            "move_15m_pct": round(abs(close-base_15)/base_15*100, 2) if base_15 else 0.0,
            "candle_body_pct": round(body/rng*100, 1) if rng else 0.0,
            "atr_normalized_move": round(body/atr, 2) if atr else 0.0,
            "price_acceleration": round(body/prev_body, 2) if prev_body else 1.0,
        }
    except Exception:
        return empty


def _bigmove_pivots(df: pd.DataFrame, left: int = 2, right: int = 2):
    """Confirmed pivots, excluding the current unconfirmed candle."""
    if df is None or len(df) < left + right + 5:
        return [], []
    d = df.reset_index(drop=True)
    highs = d["High"].astype(float).to_numpy()
    lows = d["Low"].astype(float).to_numpy()
    ph, pl = [], []
    for i in range(left, len(d) - right):
        if highs[i] >= max(highs[i-left:i]) and highs[i] > max(highs[i+1:i+right+1]):
            ph.append((i, float(highs[i])))
        if lows[i] <= min(lows[i-left:i]) and lows[i] < min(lows[i+1:i+right+1]):
            pl.append((i, float(lows[i])))
    return ph, pl


def _detect_amd_phase(df: pd.DataFrame) -> Dict[str, Any]:
    """Heuristic Accumulation-Manipulation-Distribution phase estimate from OHLCV.

    This is a candle-based estimate, not proof of institutional activity.
    Uses only available completed candles and returns a safe WAIT fallback.
    """
    result = {"phase": "INSUFFICIENT_DATA", "direction": "NONE", "signal": "WAIT", "reason": "Need at least 20 candles"}
    if df is None or len(df) < 20:
        return result
    try:
        d = df.reset_index(drop=True).copy()
        required = ["Open", "High", "Low", "Close", "Volume"]
        if any(c not in d.columns for c in required):
            result["reason"] = "Missing OHLCV columns"
            return result
        d[required] = d[required].apply(pd.to_numeric, errors="coerce")
        d = d.dropna(subset=required)
        if len(d) < 20:
            return result
        last = d.iloc[-1]
        prior = d.iloc[-13:-1]
        if len(prior) < 8:
            return result
        range_high = float(prior["High"].max())
        range_low = float(prior["Low"].min())
        close = float(last["Close"])
        high = float(last["High"])
        low = float(last["Low"])
        open_ = float(last["Open"])
        avg_volume = float(prior["Volume"].mean())
        rvol = float(last["Volume"] / avg_volume) if avg_volume > 0 else 0.0
        width_pct = ((range_high - range_low) / max((range_high + range_low) / 2.0, 1e-9)) * 100
        swept_low = low < range_low and close > range_low
        swept_high = high > range_high and close < range_high
        breakout_up = close > range_high
        breakout_down = close < range_low
        bullish_candle = close > open_
        bearish_candle = close < open_

        if swept_low:
            result.update(phase="MANIPULATION", direction="UP", signal="WATCH BUY", reason=f"Low sweep reclaimed range; RVOL {rvol:.2f}x")
        elif swept_high:
            result.update(phase="MANIPULATION", direction="DOWN", signal="WATCH SELL", reason=f"High sweep rejected back into range; RVOL {rvol:.2f}x")
        elif breakout_up and bullish_candle and rvol >= 1.2:
            result.update(phase="CONFIRMATION", direction="UP", signal="BUY WATCH", reason=f"Upside range break with bullish candle; RVOL {rvol:.2f}x")
        elif breakout_down and bearish_candle and rvol >= 1.2:
            result.update(phase="CONFIRMATION", direction="DOWN", signal="SELL WATCH", reason=f"Downside range break with bearish candle; RVOL {rvol:.2f}x")
        elif breakout_up:
            result.update(phase="DISTRIBUTION", direction="UP", signal="WAIT CONFIRMATION", reason=f"Upside break lacks volume/candle confirmation; RVOL {rvol:.2f}x")
        elif breakout_down:
            result.update(phase="DISTRIBUTION", direction="DOWN", signal="WAIT CONFIRMATION", reason=f"Downside break lacks volume/candle confirmation; RVOL {rvol:.2f}x")
        elif width_pct <= 2.0:
            result.update(phase="ACCUMULATION", direction="NONE", signal="WAIT", reason=f"Tight prior range ({width_pct:.2f}%)")
        else:
            result.update(phase="ACCUMULATION", direction="NONE", signal="WAIT", reason=f"Range-bound; waiting for sweep/breakout ({width_pct:.2f}%)")
        return result
    except Exception as e:
        return {"phase": "ERROR", "direction": "NONE", "signal": "WAIT", "reason": f"AMD error: {type(e).__name__}"}


def detect_last_consolidation(df: pd.DataFrame) -> Dict[str, Any]:
    """Find the most recent tight range immediately before the current move."""
    empty = {
        "found": False, "start_idx": None, "end_idx": None, "bars": 0,
        "high": None, "low": None, "range_pct": 0.0, "range_atr": 0.0,
    }
    if df is None or len(df) < 20:
        return empty
    try:
        d = df.reset_index(drop=True).copy()
        last_idx = len(d) - 1
        atr_s = calculate_atr(d, 14)
        atr = float(atr_s.iloc[-1]) if pd.notna(atr_s.iloc[-1]) else max(float(d["Close"].iloc[-1])*0.005, 0.01)
        best = None
        # The latest consolidation must end before the breakout candle.
        for end_idx in range(last_idx - 1, max(2, last_idx - BIGMOVE_LOOKBACK_BARS), -1):
            for bars in range(BIGMOVE_CONSOLIDATION_MIN_BARS, BIGMOVE_CONSOLIDATION_MAX_BARS + 1):
                start_idx = end_idx - bars + 1
                if start_idx < 2:
                    continue
                w = d.iloc[start_idx:end_idx+1]
                hi = float(w["High"].max()); lo = float(w["Low"].min())
                mid = (hi + lo) / 2.0
                if mid <= 0:
                    continue
                range_pct = (hi-lo)/mid*100.0
                bar_ranges = (w["High"] - w["Low"]).astype(float)
                median_bar_range = float(bar_ranges.median()) if len(bar_ranges) else 0.0
                # Tight range + no single abnormal expansion inside consolidation.
                if range_pct > BIGMOVE_MAX_RANGE_PCT:
                    continue
                if median_bar_range > atr * BIGMOVE_MAX_BAR_ATR_MULT:
                    continue
                # Prefer the latest qualifying range; allow only a small gap to breakout.
                gap = last_idx - end_idx
                if gap > 2:
                    continue
                score = (gap * 1000) + (BIGMOVE_CONSOLIDATION_MAX_BARS - bars) + range_pct
                if best is None or score < best[0]:
                    best = (score, start_idx, end_idx, bars, hi, lo, range_pct, (hi-lo)/atr if atr else 0.0)
        if best is None:
            return empty
        _, start_idx, end_idx, bars, hi, lo, range_pct, range_atr = best
        return {
            "found": True, "start_idx": int(start_idx), "end_idx": int(end_idx),
            "bars": int(bars), "high": hi, "low": lo,
            "range_pct": round(range_pct, 2), "range_atr": round(range_atr, 2),
        }
    except Exception:
        return empty


def detect_big_move_setup(df: pd.DataFrame) -> Dict[str, Any]:
    """Detect BIG BUY/BIG SELL from the latest consolidation breakout and HH/HL or LH/LL."""
    out = {
        "signal": "NO BIG MOVE", "direction": "NONE", "score": 0.0,
        "reason": "", "consolidation": detect_last_consolidation(df),
        "breakout_level": None, "breakout_move_pct": 0.0,
        "body_pct": 0.0, "body_atr": 0.0, "rvol": 0.0,
        "structure": "NONE", "hh_hl": False, "lh_ll": False,
        "current_move_pct": 0.0,
    }
    if df is None or len(df) < 25:
        out["reason"] = "Insufficient 5M candles"
        return out
    try:
        d = df.reset_index(drop=True).copy()
        c = detect_last_consolidation(d)
        if not c["found"]:
            out["reason"] = "No recent tight consolidation"
            return out

        last = d.iloc[-1]
        close = float(last["Close"]); open_ = float(last["Open"])
        high = float(last["High"]); low = float(last["Low"])
        body = abs(close-open_); rng = max(high-low, 0.0)
        atr_s = calculate_atr(d, 14)
        atr = float(atr_s.iloc[-1]) if pd.notna(atr_s.iloc[-1]) else max(close*0.005, 0.01)
        vol_avg = float(d["Volume"].iloc[-21:-1].mean()) if len(d) >= 22 else float(d["Volume"].iloc[:-1].mean())
        rvol = float(last["Volume"])/vol_avg if vol_avg > 0 else 0.0
        body_pct = body/rng*100 if rng else 0.0
        body_atr = body/atr if atr else 0.0

        buy_break = close > float(c["high"])
        sell_break = close < float(c["low"])
        breakout_level = c["high"] if buy_break else c["low"] if sell_break else None
        breakout_move_pct = abs(close-breakout_level)/breakout_level*100 if breakout_level else 0.0
        prev_close = float(d["Close"].iloc[-2])
        current_move_pct = abs(close-prev_close)/prev_close*100 if prev_close else 0.0

        # Latest confirmed HH/HL or LH/LL structure. The current breakout candle
        # is not treated as a confirmed pivot, so we never use an unfinished pivot.
        ph, pl = _bigmove_pivots(d)
        hh_hl = False; lh_ll = False
        if len(ph) >= 2 and len(pl) >= 2:
            hh_hl = ph[-1][1] > ph[-2][1] and pl[-1][1] > pl[-2][1]
            lh_ll = ph[-1][1] < ph[-2][1] and pl[-1][1] < pl[-2][1]
        structure = "HH/HL" if hh_hl else "LH/LL" if lh_ll else "NONE"

        # Score is evidence, not an EMA/RSI/AI score.
        score = 0.0
        reasons = []
        if buy_break or sell_break:
            score += 30; reasons.append("consolidation breakout")
        if body_atr >= BIGMOVE_MIN_BODY_ATR:
            score += 20; reasons.append(f"big candle {body_atr:.2f} ATR")
        if body_pct >= BIGMOVE_MIN_BODY_PCT:
            score += 10; reasons.append(f"body {body_pct:.0f}%")
        if rvol >= BIGMOVE_MIN_RVOL:
            score += 20; reasons.append(f"RVOL {rvol:.2f}x")
        if breakout_move_pct >= BIGMOVE_MIN_BREAK_PCT:
            score += 10; reasons.append(f"break {breakout_move_pct:.2f}%")
        if hh_hl or lh_ll:
            score += 10; reasons.append(structure)

        # BIG MOVE requires the directional breakout + strong candle + volume.
        core_ok = (body_atr >= BIGMOVE_MIN_BODY_ATR and body_pct >= BIGMOVE_MIN_BODY_PCT
                   and rvol >= BIGMOVE_MIN_RVOL and breakout_move_pct >= BIGMOVE_MIN_BREAK_PCT)
        if buy_break and core_ok:
            out["direction"] = "UP"
            out["structure"] = structure
            out["hh_hl"] = hh_hl
            out["signal"] = "🟢 BIG BUY" if score >= BIGMOVE_STRONG_SCORE else "🟢 BUY MOVE"
        elif sell_break and core_ok:
            out["direction"] = "DOWN"
            out["structure"] = structure
            out["lh_ll"] = lh_ll
            out["signal"] = "🔴 BIG SELL" if score >= BIGMOVE_STRONG_SCORE else "🔴 SELL MOVE"
        else:
            if not buy_break and not sell_break:
                reasons.append("no consolidation break")
            if body_atr < BIGMOVE_MIN_BODY_ATR:
                reasons.append(f"candle < {BIGMOVE_MIN_BODY_ATR:.2f} ATR")
            if body_pct < BIGMOVE_MIN_BODY_PCT:
                reasons.append(f"body < {BIGMOVE_MIN_BODY_PCT:.0f}%")
            if rvol < BIGMOVE_MIN_RVOL:
                reasons.append(f"RVOL < {BIGMOVE_MIN_RVOL:.2f}x")
            if breakout_move_pct < BIGMOVE_MIN_BREAK_PCT:
                reasons.append(f"break < {BIGMOVE_MIN_BREAK_PCT:.2f}%")

        out.update({
            "score": round(min(100.0, score), 1),
            "reason": " | ".join(reasons),
            "breakout_level": breakout_level,
            "breakout_move_pct": round(breakout_move_pct, 2),
            "body_pct": round(body_pct, 1),
            "body_atr": round(body_atr, 2),
            "rvol": round(rvol, 2),
            "current_move_pct": round(current_move_pct, 2),
            "consolidation": c,
        })
        return out
    except Exception as e:
        out["reason"] = f"BIG MOVE error: {type(e).__name__}"
        return out

def calculate_momentum_score(analysis_5m: Dict, analysis_15m: Dict, analysis_1h: Dict, movement_metrics: Dict, is_bullish: bool) -> Dict[str, Any]:
    """Calculate momentum score (0-100) based on multiple factors."""
    data_5m = analysis_5m.get("data", {})
    data_15m = analysis_15m.get("data", {})
    data_1h = analysis_1h.get("data", {})
    
    if not data_5m:
        return {"score": 0, "status": "INSUFFICIENT_DATA"}
    
    score = 50
    score_breakdown = {}
    
    try:
        # 1. PRICE MOVEMENT (20 points)
        move_5m = movement_metrics.get("move_5m_pct", 0)
        move_15m = movement_metrics.get("move_15m_pct", 0)
        
        if move_5m >= 0.5 and move_15m >= 1.0:
            score += 20
        elif move_5m >= 0.3 and move_15m >= 0.5:
            score += 15
        elif move_5m >= 0.1:
            score += 10
        else:
            score -= 5
        
        score_breakdown["price_movement"] = 20
        
        # 2. 5M MOMENTUM (15 points)
        tf_5m = data_5m.get("structure_trend", "NEUTRAL")
        if (is_bullish and tf_5m == "BULLISH") or (not is_bullish and tf_5m == "BEARISH"):
            score += 15
            score_breakdown["5m_momentum"] = 15
        else:
            score -= 10
            score_breakdown["5m_momentum"] = -10
        
        # 3. 15M TREND (15 points)
        tf_15m = data_15m.get("structure_trend", "NEUTRAL")
        if (is_bullish and tf_15m in ["BULLISH", "NEUTRAL"]) or (not is_bullish and tf_15m in ["BEARISH", "NEUTRAL"]):
            score += 15
            score_breakdown["15m_trend"] = 15
        elif (is_bullish and tf_15m == "BEARISH") or (not is_bullish and tf_15m == "BULLISH"):
            score -= 8
            score_breakdown["15m_trend"] = -8
        else:
            score += 7
            score_breakdown["15m_trend"] = 7
        
        # 4. 1H TREND (10 points)
        tf_1h = data_1h.get("structure_trend", "NEUTRAL")
        if (is_bullish and tf_1h in ["BULLISH", "NEUTRAL"]) or (not is_bullish and tf_1h in ["BEARISH", "NEUTRAL"]):
            score += 10
            score_breakdown["1h_trend"] = 10
        elif (is_bullish and tf_1h == "BEARISH") or (not is_bullish and tf_1h == "BULLISH"):
            score -= 5
            score_breakdown["1h_trend"] = -5
        
        # 5. VWAP (10 points)
        vwap = data_5m.get("vwap")
        price = data_5m.get("last_close", 0)
        if vwap is not None:
            if (is_bullish and price > vwap) or (not is_bullish and price < vwap):
                score += 10
                score_breakdown["vwap"] = 10
            else:
                score -= 5
                score_breakdown["vwap"] = -5
        
        # 6. EMA ALIGNMENT (10 points)
        ema_trend = data_5m.get("ema_trend", "NEUTRAL")
        if (is_bullish and ema_trend == "BULLISH") or (not is_bullish and ema_trend == "BEARISH"):
            score += 10
            score_breakdown["ema"] = 10
        elif (is_bullish and ema_trend == "BEARISH") or (not is_bullish and ema_trend == "BULLISH"):
            score -= 8
            score_breakdown["ema"] = -8
        else:
            score += 3
            score_breakdown["ema"] = 3
        
        # 7. BUYING/SELLING PRESSURE (10 points)
        pressure_trend = data_5m.get("pressure_trend", "NEUTRAL")
        
        if is_bullish:
            if pressure_trend == "STRONG_BUYING":
                score += 10
                score_breakdown["pressure"] = 10
            elif pressure_trend == "BUYING":
                score += 7
                score_breakdown["pressure"] = 7
            elif pressure_trend == "STRONG_SELLING":
                score -= 8
                score_breakdown["pressure"] = -8
            elif pressure_trend == "SELLING":
                score -= 3
                score_breakdown["pressure"] = -3
        else:
            if pressure_trend == "STRONG_SELLING":
                score += 10
                score_breakdown["pressure"] = 10
            elif pressure_trend == "SELLING":
                score += 7
                score_breakdown["pressure"] = 7
            elif pressure_trend == "STRONG_BUYING":
                score -= 8
                score_breakdown["pressure"] = -8
            elif pressure_trend == "BUYING":
                score -= 3
                score_breakdown["pressure"] = -3
        
        # 8. RVOL (10 points)
        rvol = data_5m.get("rvol", 1.0)
        if rvol >= 2.0:
            score += 10
            score_breakdown["rvol"] = 10
        elif rvol >= 1.5:
            score += 8
            score_breakdown["rvol"] = 8
        elif rvol >= 1.2:
            score += 5
            score_breakdown["rvol"] = 5
        elif rvol < 0.8:
            score -= 5
            score_breakdown["rvol"] = -5
        else:
            score_breakdown["rvol"] = 0
        
        score = max(0, min(100, score))
        
        if score >= 85:
            if is_bullish:
                status = "🟢 STRONG MOMENTUM BUY"
            else:
                status = "🔴 STRONG MOMENTUM SELL"
        elif score >= 70:
            if is_bullish:
                status = "🟢 MOMENTUM BUY"
            else:
                status = "🔴 MOMENTUM SELL"
        elif score >= 55:
            if is_bullish:
                status = "🟡 DEVELOPING BUY"
            else:
                status = "🟠 DEVELOPING SELL"
        else:
            status = "NONE"
        
        return {
            "score": round(score, 1),
            "status": status,
            "breakdown": score_breakdown,
        }
    
    except Exception as e:
        return {"score": 0, "status": "ERROR"}

# ════════════════════════════════════════════════════════════════════════════════
# NEXT CANDLE BIAS CALCULATION (NEW)
# ════════════════════════════════════════════════════════════════════════════════
def calculate_next_candle_bias(df, timeframe_analysis: Dict[str, Any]) -> Dict[str, Any]:
    """Calculate Next Candle Bias using weighted scoring from confirmed data."""
    if df is None or len(df) < 5:
        return {"bias": "NEUTRAL", "confidence": 0.0}
    
    try:
        data = timeframe_analysis.get("data", {})
        if not data:
            return {"bias": "NEUTRAL", "confidence": 0.0}
        
        buy_score = 50.0
        scores_count = 0
        
        last_close = float(df["Close"].iloc[-1])
        last_open = float(df["Open"].iloc[-1])
        
        if last_close > last_open:
            buy_score += 8
            scores_count += 1
        elif last_close < last_open:
            buy_score -= 8
            scores_count += 1
        
        pressure_trend = data.get("pressure_trend", "NEUTRAL")
        if pressure_trend == "STRONG_BUYING":
            buy_score += 12
        elif pressure_trend == "BUYING":
            buy_score += 8
        elif pressure_trend == "STRONG_SELLING":
            buy_score -= 12
        elif pressure_trend == "SELLING":
            buy_score -= 8
        scores_count += 1
        
        vwap = data.get("vwap")
        if vwap is not None:
            if last_close > vwap:
                buy_score += 6
            elif last_close < vwap:
                buy_score -= 6
            scores_count += 1
        
        ema_trend = data.get("ema_trend", "NEUTRAL")
        if ema_trend == "BULLISH":
            buy_score += 7
        elif ema_trend == "BEARISH":
            buy_score -= 7
        scores_count += 1
        
        rsi = data.get("rsi", 50)
        if rsi > 70:
            buy_score -= 4
        elif rsi < 30:
            buy_score += 4
        elif rsi > 60:
            buy_score += 3
        elif rsi < 40:
            buy_score -= 3
        scores_count += 1
        
        if data.get("macd_bullish"):
            buy_score += 5
        else:
            buy_score -= 5
        scores_count += 1
        
        structure_trend = data.get("structure_trend", "NEUTRAL")
        if structure_trend == "BULLISH":
            buy_score += 6
        elif structure_trend == "BEARISH":
            buy_score -= 6
        scores_count += 1
        
        if data.get("bullish_choch") or data.get("bullish_mss") or data.get("bullish_cisd"):
            buy_score += 10
        elif data.get("bearish_choch") or data.get("bearish_mss") or data.get("bearish_cisd"):
            buy_score -= 10
        scores_count += 1
        
        rvol = data.get("rvol", 1.0)
        if rvol > 1.5:
            if buy_score > 50:
                buy_score += 4
            else:
                buy_score -= 4
        elif rvol < 0.8:
            buy_score = buy_score * 0.95
        scores_count += 1
        
        buy_score = max(0, min(100, buy_score))
        confidence = round(abs(buy_score - 50) * 0.8, 1)
        confidence = max(0, min(100, confidence))
        
        if buy_score >= 65:
            bias = "🟢 BUY"
        elif buy_score <= 35:
            bias = "🔴 SELL"
        else:
            bias = "🟡 NEUTRAL"
        
        return {
            "bias": bias,
            "confidence": confidence,
            "score": buy_score,
        }
    
    except Exception as e:
        return {"bias": "NEUTRAL", "confidence": 0.0}

# ════════════════════════════════════════════════════════════════════════════════
# NEW INDICATORS (MULTI-TIMEFRAME)
# ════════════════════════════════════════════════════════════════════════════════
def calculate_vwap(df) -> pd.Series:
    """Calculate VWAP"""
    if "Volume" not in df.columns or len(df) == 0:
        return pd.Series([np.nan] * len(df), index=df.index)
    
    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    close = df["Close"].astype(float)
    volume = df["Volume"].astype(float)
    
    typical_price = (high + low + close) / 3
    vwap = (typical_price * volume).cumsum() / volume.cumsum()
    return vwap.fillna(method="ffill").fillna(close)

def calculate_ema(close, period: int = 9) -> pd.Series:
    """Calculate EMA"""
    return close.ewm(span=period, adjust=False).mean()

def find_swing_highs_lows(df, lookback: int = SWING_LOOKBACK_PERIODS) -> Dict[str, Any]:
    """Return the latest confirmed swing high/low using only CLOSED candles."""
    empty = {"swing_high": None, "swing_high_idx": None,
             "swing_high_bars_ago": None, "swing_low": None,
             "swing_low_idx": None, "swing_low_bars_ago": None}
    if df is None or len(df) < 7:
        return empty
    d = df.tail(max(lookback, 7)).reset_index(drop=True).copy()
    highs, lows = d["High"].astype(float).to_numpy(), d["Low"].astype(float).to_numpy()
    pivot_highs, pivot_lows = [], []
    for i in range(2, len(d) - 2):
        if highs[i] > highs[i-1] and highs[i] >= highs[i-2] and highs[i] > highs[i+1] and highs[i] >= highs[i+2]:
            pivot_highs.append(i)
        if lows[i] < lows[i-1] and lows[i] <= lows[i-2] and lows[i] < lows[i+1] and lows[i] <= lows[i+2]:
            pivot_lows.append(i)
    if pivot_highs:
        i = pivot_highs[-1]
        empty.update(swing_high=float(highs[i]), swing_high_idx=int(i), swing_high_bars_ago=int(len(d)-1-i))
    if pivot_lows:
        i = pivot_lows[-1]
        empty.update(swing_low=float(lows[i]), swing_low_idx=int(i), swing_low_bars_ago=int(len(d)-1-i))
    return empty

def _confirmed_pivots(df, left: int = 2, right: int = 2):
    """Return confirmed pivot highs/lows."""
    if df is None or len(df) < left + right + 3:
        return [], []
    d = df.reset_index(drop=True)
    highs, lows = d["High"].astype(float).to_numpy(), d["Low"].astype(float).to_numpy()
    ph, pl = [], []
    for i in range(left, len(d) - right):
        if highs[i] >= max(highs[i-left:i]) and highs[i] > max(highs[i+1:i+right+1]):
            ph.append((i, float(highs[i])))
        if lows[i] <= min(lows[i-left:i]) and lows[i] < min(lows[i+1:i+right+1]):
            pl.append((i, float(lows[i])))
    return ph, pl

def detect_structure(df) -> Dict[str, Any]:
    """Detect HH/HL/LH/LL from confirmed pivots only."""
    ph, pl = _confirmed_pivots(df)
    result = {"type":"UNKNOWN", "trend":"NEUTRAL", "current_high":None,
              "current_low":None, "prev_high":None, "prev_low":None, "strength": 0}
    if len(ph) >= 2:
        result["prev_high"], result["current_high"] = ph[-2][1], ph[-1][1]
    if len(pl) >= 2:
        result["prev_low"], result["current_low"] = pl[-2][1], pl[-1][1]
    if len(ph) >= 2 and len(pl) >= 2:
        hh = ph[-1][1] > ph[-2][1]
        hl = pl[-1][1] > pl[-2][1]
        lh = ph[-1][1] < ph[-2][1]
        ll = pl[-1][1] < pl[-2][1]
        if hh and hl:
            result["type"], result["trend"] = "HH/HL", "BULLISH"
            result["strength"] = min(100, abs((ph[-1][1] - ph[-2][1]) / ph[-2][1] * 100) * 10)
        elif lh and ll:
            result["type"], result["trend"] = "LH/LL", "BEARISH"
            result["strength"] = min(100, abs((pl[-1][1] - pl[-2][1]) / pl[-2][1] * 100) * 10)
        elif hh:
            result["type"], result["trend"] = "HH", "BULLISH"
            result["strength"] = 60
        elif ll:
            result["type"], result["trend"] = "LL", "BEARISH"
            result["strength"] = 60
        elif hl:
            result["type"], result["trend"] = "HL", "BULLISH"
            result["strength"] = 40
        elif lh:
            result["type"], result["trend"] = "LH", "BEARISH"
            result["strength"] = 40
    return result

def detect_choch(df) -> Dict[str, Any]:
    """Detect a NEW confirmed CHoCH on the latest CLOSED candle only."""
    ph, pl = _confirmed_pivots(df)
    out = {"bullish_choch":False, "bearish_choch":False, "choch_price":None,
           "choch_type":"NONE", "confirmation":"NONE"}
    if len(ph) < 2 or len(pl) < 2 or len(df) < 10:
        out["confirmation"] = "PENDING"
        return out
    prev_close = float(df["Close"].iloc[-2])
    close = float(df["Close"].iloc[-1])
    bearish_structure = ph[-1][1] < ph[-2][1] and pl[-1][1] < pl[-2][1]
    bullish_structure = ph[-1][1] > ph[-2][1] and pl[-1][1] > pl[-2][1]
    
    min_move = max(ph[-1][1] * 0.001, 0.5)
    
    if bearish_structure and prev_close <= ph[-1][1] and close > (ph[-1][1] + min_move):
        out.update(bullish_choch=True, choch_price=ph[-1][1], choch_type="BULLISH_CHoCH", confirmation="CONFIRMED")
    elif bullish_structure and prev_close >= pl[-1][1] and close < (pl[-1][1] - min_move):
        out.update(bearish_choch=True, choch_price=pl[-1][1], choch_type="BEARISH_CHoCH", confirmation="CONFIRMED")
    return out

def detect_cisd(df: pd.DataFrame) -> Dict[str, Any]:
    """Detect a NEW confirmed CISD event on the latest CLOSED candle only."""
    result = {"bullish_cisd": False, "bearish_cisd": False, "cisd_type": "NONE", "cisd_price": None}
    if df is None or len(df) < 5:
        return result
    d = df.reset_index(drop=True)
    last = d.iloc[-1]
    prev_close = float(d["Close"].iloc[-2])
    prior = d.iloc[:-1]
    
    bearish = prior[prior["Close"] < prior["Open"]].tail(3)
    bullish = prior[prior["Close"] > prior["Open"]].tail(3)
    
    min_move = float(last["Close"]) * 0.001
    
    if not bearish.empty:
        level = float(bearish.iloc[-1]["High"])
        if prev_close <= level and float(last["Close"]) > (level + min_move):
            result.update(bullish_cisd=True, cisd_type="BULLISH_CISD", cisd_price=float(last["Close"]))
    
    if not bullish.empty and not result["bullish_cisd"]:
        level = float(bullish.iloc[-1]["Low"])
        if prev_close >= level and float(last["Close"]) < (level - min_move):
            result.update(bearish_cisd=True, cisd_type="BEARISH_CISD", cisd_price=float(last["Close"]))
    return result

def detect_mss(df) -> Dict[str, Any]:
    """Detect a NEW confirmed MSS event on the latest CLOSED candle only."""
    ph, pl = _confirmed_pivots(df)
    out = {"bullish_mss":False, "bearish_mss":False, "mss_type":"NONE", "confirmation":"NONE"}
    if len(ph) < 2 or len(pl) < 2 or len(df) < 10:
        out["confirmation"] = "PENDING"
        return out
    prev_close = float(df["Close"].iloc[-2])
    close = float(df["Close"].iloc[-1])
    bearish_structure = ph[-1][1] < ph[-2][1] and pl[-1][1] < pl[-2][1]
    bullish_structure = ph[-1][1] > ph[-2][1] and pl[-1][1] > pl[-2][1]
    
    min_move = max(ph[-1][1] * 0.001, 0.5)
    
    if bearish_structure and prev_close <= ph[-1][1] and close > (ph[-1][1] + min_move):
        out.update(bullish_mss=True, mss_type="BULLISH_MSS", confirmation="CONFIRMED")
    elif bullish_structure and prev_close >= pl[-1][1] and close < (pl[-1][1] - min_move):
        out.update(bearish_mss=True, mss_type="BEARISH_MSS", confirmation="CONFIRMED")
    return out

# ════════════════════════════════════════════════════════════════════════════════
# SYMBOL LOADING (RETAINED WITH ENHANCEMENT FOR F&O)
# ════════════════════════════════════════════════════════════════════════════════
_VALID_EQ_SYMBOL_RE = re.compile(r"^NSE:[A-Z0-9&\-]+-EQ$")
_FO_EQUITY_PATTERN = re.compile(r"^NSE:[A-Z0-9&\-]+-EQ$")

def _validate_symbols(symbols) -> List[str]:
    seen = set()
    valid = []
    for s in symbols:
        if not isinstance(s, str):
            continue
        s = s.strip().upper()
        if not s or s in seen:
            continue
        if not _VALID_EQ_SYMBOL_RE.match(s):
            continue
        seen.add(s)
        valid.append(s)
    return valid

@st.cache_data(ttl=60 * 60 * 12)
def load_nse_equity_symbols() -> List[str]:
    try:
        resp = requests.get(FYERS_NSE_CM_SYMBOL_MASTER, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        st.error(f"Could not download Fyers symbol master: {e}")
        return []
    lines = [ln for ln in resp.text.strip().split("\n") if ln.strip()]
    if not lines:
        return []
    sample = lines[:min(500, len(lines))]
    split_sample = [ln.split(",") for ln in sample]
    max_cols = max((len(p) for p in split_sample), default=0)
    best_col, best_hits = None, 0
    for col_idx in range(max_cols):
        hits = sum(1 for parts in split_sample if len(parts) > col_idx and parts[col_idx].strip().startswith("NSE:") and parts[col_idx].strip().endswith("-EQ"))
        if hits > best_hits:
            best_col, best_hits = col_idx, hits
    if best_col is None or best_hits == 0:
        st.error("Could not locate trading-symbol column.")
        return []
    symbols = []
    for line in lines:
        parts = line.split(",")
        if len(parts) <= best_col:
            continue
        sym = parts[best_col].strip()
        if sym.startswith("NSE:") and sym.endswith("-EQ"):
            symbols.append(sym)
    return sorted(set(_validate_symbols(symbols)))

@st.cache_data(ttl=60 * 60 * 12)
def load_fo_stocks() -> List[str]:
    """Load NSE equity symbols that have active equity-derivative contracts."""
    try:
        cm_symbols = set(load_nse_equity_symbols())
        if not cm_symbols:
            return []

        urls = [
            "https://public.fyers.in/sym_details/NSE_FO.csv",
            "http://public.fyers.in/sym_details/NSE_FO.csv",
        ]
        text = None
        last_error = None
        for url in urls:
            try:
                r = requests.get(
                    url,
                    timeout=30,
                    headers={"User-Agent": "Mozilla/5.0", "Accept": "text/csv,*/*"},
                )
                r.raise_for_status()
                if r.text and len(r.text) > 100:
                    text = r.text
                    break
            except Exception as exc:
                last_error = exc

        if not text:
            logging.warning("F&O symbol master unavailable: %s", last_error)
            return []

        fo_underlyings = set()
        reader = csv.reader(io.StringIO(text))
        for row in reader:
            if len(row) < 14:
                continue

            short_sym = str(row[13]).strip().upper()
            contract_symbol = str(row[9]).strip().upper() if len(row) > 9 else ""
            exchange = str(row[10]).strip() if len(row) > 10 else ""

            if exchange not in {"10", "NSE"}:
                continue
            if not short_sym or short_sym in {"NONE", "NAN"}:
                continue

            if contract_symbol.startswith("NSE:") and short_sym:
                candidate = f"NSE:{short_sym}-EQ"
                if candidate in cm_symbols:
                    fo_underlyings.add(candidate)

        result = sorted(fo_underlyings)
        logging.info("Loaded %d NSE F&O equity underlyings", len(result))
        return result

    except Exception as e:
        logging.exception("F&O symbol loading failed: %s", e)
        return []

# ════════════════════════════════════════════════════════════════════════════════
# SAFE HISTORY FETCH (RETAINED)
# ════════════════════════════════════════════════════════════════════════════════
_HISTORY_MAX_RETRIES = 3
_HISTORY_BASE_DELAY_SECONDS = 1.0

def _safe_history(fyers, params: dict, max_retries: int = _HISTORY_MAX_RETRIES, base_delay: float = _HISTORY_BASE_DELAY_SECONDS):
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
                    if not isinstance(candles, list):
                        last_err = "malformed candle data"
                    else:
                        return resp, None
                else:
                    message = str(resp.get("message", status or "unknown"))
                    if "rate" in message.lower() or "limit" in message.lower():
                        last_err = f"rate limited: {message}"
                        time.sleep(base_delay * attempt * 2)
                        continue
                    return None, message
        if attempt < max_retries:
            time.sleep(base_delay * attempt)
    return None, f"{symbol}: {last_err} (after {max_retries} attempts)"

# ════════════════════════════════════════════════════════════════════════════════
# SCAN STATS (ORIGINAL)
# ════════════════════════════════════════════════════════════════════════════════
class ScanStats:
    def __init__(self, total: int):
        self.total = total
        self.scanned = 0
        self.successful = 0
        self.skipped = 0
        self.failed = 0
        self._start = time.time()

    def record(self, has_result: bool, has_error: bool) -> None:
        self.scanned += 1
        if has_result:
            self.successful += 1
        elif has_error:
            self.failed += 1
        else:
            self.skipped += 1

    @property
    def elapsed_seconds(self) -> float:
        return time.time() - self._start

def _display_scan_summary(stats: "ScanStats") -> None:
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Total Stocks", stats.total)
    c2.metric("Scanned", stats.scanned)
    c3.metric("Successful", stats.successful)
    c4.metric("Skipped", stats.skipped)
    c5.metric("Failed", stats.failed)
    c6.metric("Scan Time", f"{stats.elapsed_seconds:.1f}s")

# ════════════════════════════════════════════════════════════════════════════════
# TIMEFRAME DATA FETCHER (NEW)
# ════════════════════════════════════════════════════════════════════════════════
def _fetch_timeframe_data(fyers, symbol, resolution: str, lookback_days: int = 30) -> Optional[pd.DataFrame]:
    """Fetch OHLCV data for a specific timeframe."""
    date_from = (datetime.today() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    date_to = datetime.today().strftime("%Y-%m-%d")
    
    resp, err = _safe_history(fyers, {
        "symbol": symbol,
        "resolution": resolution,
        "date_format": "1",
        "range_from": date_from,
        "range_to": date_to,
        "cont_flag": "1",
    })
    
    if err or not resp:
        return None
    
    candles = resp.get("candles")
    if not candles or len(candles) < 10:
        return None
    
    try:
        df = pd.DataFrame(candles, columns=["Time", "Open", "High", "Low", "Close", "Volume"])
        df["Time"] = pd.to_datetime(df["Time"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
        df[["Open", "High", "Low", "Close", "Volume"]] = df[["Open", "High", "Low", "Close", "Volume"]].apply(pd.to_numeric, errors="coerce")
        df = df.dropna(subset=["Open", "High", "Low", "Close"]).sort_values("Time").reset_index(drop=True)
        
        if len(df) < 10:
            return None
        
        if len(df) > 1:
            last_time = df["Time"].iloc[-1]
            candle_age = (_now_ist() - last_time).total_seconds() / 60
            res_minutes = 1440 if resolution == "D" else int(resolution)
            if candle_age < res_minutes + 1:
                df = df.iloc[:-1].reset_index(drop=True)
        
        if len(df) < 10:
            return None
        
        return df
    
    except Exception as e:
        return None

# ════════════════════════════════════════════════════════════════════════════════
# TIMEFRAME ANALYSIS ENGINE (NEW - WITH PRESSURE)
# ════════════════════════════════════════════════════════════════════════════════
def analyze_timeframe(fyers, symbol: str, resolution: str) -> Dict[str, Any]:
    """Analyze a specific timeframe."""
    df = _fetch_timeframe_data(fyers, symbol, resolution, lookback_days=30)
    
    if df is None or len(df) < 10:
        return {
            "timeframe": resolution,
            "status": "DATA_UNAVAILABLE",
            "data": None,
        }
    
    try:
        rsi = calculate_rsi(df["Close"])
        macd_line, macd_sig, macd_hist = calculate_macd(df["Close"])
        atr = calculate_atr(df)
        vwap = calculate_vwap(df)
        ema9 = calculate_ema(df["Close"], 9)
        ema21 = calculate_ema(df["Close"], 21)
        ema50 = calculate_ema(df["Close"], 50)
        ema200 = calculate_ema(df["Close"], 200)
        
        pressure = calculate_buying_selling_pressure(df)
        order_flow = calculate_order_flow_delta(df, lookback=20)
        
        structure = detect_structure(df)
        choch = detect_choch(df)
        mss = detect_mss(df)
        cisd = detect_cisd(df)
        swings = find_swing_highs_lows(df)
        
        vol_avg20 = float(df["Volume"].tail(20).mean()) if "Volume" in df.columns else 0
        last_vol = float(df["Volume"].iloc[-1]) if "Volume" in df.columns else 0
        rvol = round(last_vol / vol_avg20, 2) if vol_avg20 > 0 else 0.0
        
        last_close = float(df["Close"].iloc[-1])
        last_high = float(df["High"].iloc[-1])
        last_low = float(df["Low"].iloc[-1])
        last_open = float(df["Open"].iloc[-1])
        
        ema_trend = "BULLISH" if ema9.iloc[-1] > ema21.iloc[-1] > ema50.iloc[-1] else "BEARISH" if ema9.iloc[-1] < ema21.iloc[-1] < ema50.iloc[-1] else "NEUTRAL"
        
        rsi_val = float(rsi.iloc[-1])
        rsi_overbought = rsi_val > 70
        rsi_oversold = rsi_val < 30
        
        macd_bullish = macd_line.iloc[-1] > macd_sig.iloc[-1]
        candle_date, candle_close_time = _candle_signal_timestamp(df, is_daily=False, resolution=resolution)
        
        return {
            "timeframe": resolution,
            "status": "OK",
            "data": {
                "last_close": last_close,
                "last_high": last_high,
                "last_low": last_low,
                "last_open": last_open,
                "signal_candle_date": candle_date,
                "signal_candle_time": candle_close_time,
                "signal_generated_at": _generated_timestamp(),
                "structure_type": structure["type"],
                "structure_trend": structure["trend"],
                "structure_strength": round(structure.get("strength", 0), 1),
                "current_high": structure["current_high"],
                "current_low": structure["current_low"],
                "prev_high": structure["prev_high"],
                "prev_low": structure["prev_low"],
                "bullish_choch": choch["bullish_choch"],
                "bearish_choch": choch["bearish_choch"],
                "choch_price": choch["choch_price"],
                "choch_type": choch["choch_type"],
                "bullish_mss": mss["bullish_mss"],
                "bearish_mss": mss["bearish_mss"],
                "mss_type": mss["mss_type"],
                "bullish_cisd": cisd["bullish_cisd"],
                "bearish_cisd": cisd["bearish_cisd"],
                "cisd_type": cisd["cisd_type"],
                "cisd_price": cisd["cisd_price"],
                "swing_high": swings["swing_high"],
                "swing_low": swings["swing_low"],
                "swing_high_bars_ago": swings["swing_high_bars_ago"],
                "swing_low_bars_ago": swings["swing_low_bars_ago"],
                "vwap": float(vwap.iloc[-1]) if len(vwap) > 0 else None,
                "ema9": float(ema9.iloc[-1]) if len(ema9) > 0 else None,
                "ema21": float(ema21.iloc[-1]) if len(ema21) > 0 else None,
                "ema50": float(ema50.iloc[-1]) if len(ema50) > 0 else None,
                "ema200": float(ema200.iloc[-1]) if len(ema200) > 0 else None,
                "ema_trend": ema_trend,
                "rsi": round(rsi_val, 1),
                "rsi_overbought": rsi_overbought,
                "rsi_oversold": rsi_oversold,
                "macd_bullish": macd_bullish,
                "macd_value": round(float(macd_line.iloc[-1]), 4),
                "rvol": rvol,
                "atr": round(float(atr.iloc[-1]), 2),
                "buying_pressure": pressure["buying_pressure"],
                "selling_pressure": pressure["selling_pressure"],
                "pressure_trend": pressure["trend"],
                "buying_volume": pressure["buying_volume"],
                "selling_volume": pressure["selling_volume"],
                "pressure_ratio": pressure["pressure_ratio"],
                "estimated_buy_volume": order_flow["estimated_buy_volume"],
                "estimated_sell_volume": order_flow["estimated_sell_volume"],
                "volume_delta": order_flow["volume_delta"],
                "delta_pct": order_flow["delta_pct"],
                "cumulative_delta": order_flow["cumulative_delta"],
                "order_flow_signal": order_flow["order_flow_signal"],
                "delta_divergence": order_flow["delta_divergence"],
                "last_candle_delta": order_flow["last_candle_delta"],
            },
            "df": df,
        }
    
    except Exception as e:
        return {
            "timeframe": resolution,
            "status": "ERROR",
            "error": str(e),
            "data": None,
        }

# ════════════════════════════════════════════════════════════════════════════════
# OPTIONS CHAIN ANALYSIS (NEW)
# ════════════════════════════════════════════════════════════════════════════════
def _fyers_optionchain_request(fyers, symbol: str, strikecount: int = OPTIONS_STRIKE_COUNT, timestamp: str = "", greeks: bool = True):
    """Call FYERS v3 option-chain endpoint directly, with SDK fallback."""
    app_id = getattr(fyers, "client_id", None) or FYERS_APP_ID
    token = getattr(fyers, "token", None) or os.environ.get("FYERS_ACCESS_TOKEN", "")
    if app_id and token:
        headers = {"Authorization": f"{app_id}:{token}"}
        params = {"symbol": symbol, "strikecount": min(int(strikecount), 50), "greeks": "1" if greeks else "0"}
        if timestamp:
            params["timestamp"] = str(timestamp)
        r = requests.get("https://api-t1.fyers.in/data/options-chain-v3", headers=headers, params=params, timeout=OPTIONS_HTTP_TIMEOUT)
        r.raise_for_status()
        return r.json()
    fn = getattr(fyers, "option_chain", None) or getattr(fyers, "optionchain", None)
    if fn:
        try:
            return fn(symbol=symbol, strikecount=min(int(strikecount), 50), timestamp=timestamp, greeks=greeks)
        except TypeError:
            return fn(data={"symbol":symbol, "strikecount":min(int(strikecount),50), "timestamp":timestamp, "greeks":greeks})
    raise RuntimeError("FYERS option-chain API is not available")

def _calculate_max_pain(chain_rows):
    strikes = sorted({float(x.get("strike_price")) for x in chain_rows if x.get("option_type") in ("CE", "PE") and x.get("strike_price") not in (None, -1)})
    if not strikes:
        return None
    ce = {(float(x.get("strike_price")), float(x.get("oi",0) or 0)) for x in chain_rows if x.get("option_type") == "CE"}
    pe = {(float(x.get("strike_price")), float(x.get("oi",0) or 0)) for x in chain_rows if x.get("option_type") == "PE"}
    ce_map, pe_map = dict(ce), dict(pe)
    pains = {}
    for settle in strikes:
        pain = sum(max(settle-k,0)*oi for k,oi in ce_map.items()) + sum(max(k-settle,0)*oi for k,oi in pe_map.items())
        pains[settle] = pain
    return min(pains, key=pains.get) if pains else None

def fetch_options_chain_data(fyers, symbol: str, expiry_timestamp: str = "") -> Dict[str, Any]:
    """Fetch live FYERS v3 options chain; never fabricate missing data."""
    empty = {"status":"DATA_UNAVAILABLE", "message":"No live option-chain data", "expiry":None, "spot":None,
             "atm_strike":None, "ce_oi":None, "pe_oi":None, "ce_oi_change":None, "pe_oi_change":None,
             "pcr":None, "max_pain":None, "ce_volume":None, "pe_volume":None, "call_writing":False,
             "put_writing":False, "call_unwinding":False, "put_unwinding":False, "options_bias":"NEUTRAL", "chain":[], "expiry_data":[]}
    try:
        resp = _fyers_optionchain_request(fyers, symbol, timestamp=expiry_timestamp)
        if not isinstance(resp, dict) or resp.get("s") not in (None, "ok"):
            empty["message"] = str(resp.get("message", "Option-chain request failed")) if isinstance(resp, dict) else "Invalid response"
            return empty
        data = resp.get("data", resp)
        chain = data.get("optionsChain") or []
        expiry_data = data.get("expiryData") or []
        if not expiry_timestamp and expiry_data:
            nearest = expiry_data[0]
            expiry_timestamp = str(nearest.get("expiry", ""))
            if expiry_timestamp:
                exp_resp = _fyers_optionchain_request(fyers, symbol, timestamp=expiry_timestamp)
                if isinstance(exp_resp, dict) and exp_resp.get("s") in (None, "ok"):
                    new_data = exp_resp.get("data", exp_resp)
                    if isinstance(new_data, dict) and new_data.get("optionsChain"):
                        data, chain = new_data, new_data["optionsChain"]
        spot_row = next((x for x in chain if x.get("option_type", "") == ""), None)
        spot = float(spot_row.get("ltp")) if spot_row and spot_row.get("ltp") is not None else None
        legs = [x for x in chain if x.get("option_type") in ("CE","PE") and x.get("strike_price") not in (None,-1)]
        strikes = sorted({float(x["strike_price"]) for x in legs})
        atm = min(strikes, key=lambda k: abs(k-spot)) if strikes and spot is not None else None
        # Prefer provided aggregate OI, otherwise sum the returned strikes.
        # Restrict interpretation to returned strikes when aggregates are unavailable.
        call_oi_raw = data.get("callOi")
        put_oi_raw = data.get("putOi")
        call_oi = float(call_oi_raw) if call_oi_raw is not None else sum(float(x.get("oi", 0) or 0) for x in legs if x.get("option_type") == "CE")
        put_oi = float(put_oi_raw) if put_oi_raw is not None else sum(float(x.get("oi", 0) or 0) for x in legs if x.get("option_type") == "PE")
        if not legs:
            empty["message"] = "FYERS returned no CE/PE contracts; check symbol, expiry and data permissions"
            return empty
        call_chg = sum(float(x.get("oich",0) or 0) for x in legs if x.get("option_type")=="CE")
        put_chg = sum(float(x.get("oich",0) or 0) for x in legs if x.get("option_type")=="PE")
        ce_vol = sum(float(x.get("volume",0) or 0) for x in legs if x.get("option_type")=="CE")
        pe_vol = sum(float(x.get("volume",0) or 0) for x in legs if x.get("option_type")=="PE")
        near = [x for x in legs if atm is not None and abs(float(x["strike_price"])-atm) <= max((strikes[1]-strikes[0]) if len(strikes)>1 else 1, 1)*3]
        ce_writing = sum(1 for x in near if x.get("option_type")=="CE" and float(x.get("oich",0) or 0)>0 and float(x.get("ltpch",0) or 0)<0)
        pe_writing = sum(1 for x in near if x.get("option_type")=="PE" and float(x.get("oich",0) or 0)>0 and float(x.get("ltpch",0) or 0)<0)
        ce_unwind = sum(1 for x in near if x.get("option_type")=="CE" and float(x.get("oich",0) or 0)<0 and float(x.get("ltpch",0) or 0)>0)
        pe_unwind = sum(1 for x in near if x.get("option_type")=="PE" and float(x.get("oich",0) or 0)<0 and float(x.get("ltpch",0) or 0)>0)
        pcr = put_oi/call_oi if call_oi > 0 else None
        bull = (pcr is not None and pcr >= 1.05) or pe_writing > ce_writing
        bear = (pcr is not None and pcr <= 0.80) or ce_writing > pe_writing
        if bull and not bear: bias = "🟢 BULLISH"
        elif bear and not bull: bias = "🔴 BEARISH"
        else: bias = "🟡 NEUTRAL"
        return {"status":"OK", "message":"Live FYERS option chain", "expiry": expiry_timestamp or (expiry_data[0].get("date") if expiry_data else None),
                "spot":spot, "atm_strike":atm, "ce_oi":call_oi, "pe_oi":put_oi, "ce_oi_change":call_chg,
                "pe_oi_change":put_chg, "pcr":round(pcr,2) if pcr is not None else None,
                "max_pain":_calculate_max_pain(legs), "ce_volume":ce_vol, "pe_volume":pe_vol,
                "call_writing":ce_writing>0, "put_writing":pe_writing>0, "call_unwinding":ce_unwind>0,
                "put_unwinding":pe_unwind>0, "options_bias":bias, "chain":legs, "expiry_data":expiry_data}
    except Exception as e:
        empty["status"] = "ERROR"; empty["message"] = str(e)[:180]
        return empty

# ════════════════════════════════════════════════════════════════════════════════
# STRICT MULTI-TIMEFRAME SIGNAL VALIDATION ENGINE (FIXED)
# ════════════════════════════════════════════════════════════════════════════════
def calculate_master_signal(symbol: str, analysis_5m: Dict, analysis_15m: Dict, analysis_1h: Dict, options_data: Dict = None) -> Dict[str, Any]:
    """STRICT master signal validation engine."""
    if options_data is None:
        options_data = {"status":"DATA_UNAVAILABLE", "options_bias":"NEUTRAL"}
    
    data_5m = analysis_5m.get("data") if analysis_5m.get("status") == "OK" else None
    data_15m = analysis_15m.get("data") if analysis_15m.get("status") == "OK" else None
    data_1h = analysis_1h.get("data") if analysis_1h.get("status") == "OK" else None
    
    if not data_5m or not data_15m or not data_1h:
        return {
            "final_signal": "🟡 NEUTRAL",
            "confidence": 0.0,
            "entry": None,
            "stop_loss": None,
            "target1": None,
            "target2": None,
            "rr_ratio": None,
            "scores": {"5m": 0, "15m": 0, "1h": 0, "pressure": 0, "options": 0},
            "signal_reason": "Insufficient timeframe data",
        }
    
    def classify_tf_direction(data: Dict) -> str:
        trend = data.get("structure_trend", "NEUTRAL")
        if trend == "BULLISH":
            return "BULLISH"
        elif trend == "BEARISH":
            return "BEARISH"
        else:
            return "NEUTRAL"
    
    tf_5m = classify_tf_direction(data_5m)
    tf_15m = classify_tf_direction(data_15m)
    tf_1h = classify_tf_direction(data_1h)
    
    is_bullish_aligned = (
        (tf_5m == "BULLISH") and
        (tf_15m in ["BULLISH", "NEUTRAL"]) and
        (tf_1h != "BEARISH")
    )
    
    is_bearish_aligned = (
        (tf_5m == "BEARISH") and
        (tf_15m in ["BEARISH", "NEUTRAL"]) and
        (tf_1h != "BULLISH")
    )
    
    hard_conflict = (tf_5m == "BULLISH" and tf_1h == "BEARISH") or (tf_5m == "BEARISH" and tf_1h == "BULLISH")
    
    if hard_conflict or (not is_bullish_aligned and not is_bearish_aligned):
        return {
            "final_signal": "🟡 NEUTRAL",
            "confidence": 20.0 if hard_conflict else 35.0,
            "entry": None,
            "stop_loss": None,
            "target1": None,
            "target2": None,
            "rr_ratio": None,
            "scores": {
                "5m": 50,
                "15m": 50,
                "1h": 50,
                "pressure": 50,
                "options": 50,
            },
            "signal_reason": f"Timeframe conflict: 5M {tf_5m} vs 15M {tf_15m} vs 1H {tf_1h}",
        }
    
    pressure_trend = data_5m.get("pressure_trend", "NEUTRAL")
    pressure_buy = pressure_trend in ["BUYING", "STRONG_BUYING"]
    pressure_sell = pressure_trend in ["SELLING", "STRONG_SELLING"]
    pressure_strong_buy = pressure_trend == "STRONG_BUYING"
    pressure_strong_sell = pressure_trend == "STRONG_SELLING"
    
    if is_bullish_aligned and not pressure_buy:
        is_bullish_aligned = False
    
    if is_bearish_aligned and not pressure_sell:
        is_bearish_aligned = False
    
    if not is_bullish_aligned and not is_bearish_aligned:
        return {
            "final_signal": "🟡 NEUTRAL",
            "confidence": 30.0,
            "entry": None,
            "stop_loss": None,
            "target1": None,
            "target2": None,
            "rr_ratio": None,
            "scores": {
                "5m": 50,
                "15m": 50,
                "1h": 50,
                "pressure": 50,
                "options": 50,
            },
            "signal_reason": f"Pressure conflict: {pressure_trend}",
        }
    
    last_close = data_5m.get("last_close", 0)
    vwap = data_5m.get("vwap")
    
    if vwap is not None:
        price_above_vwap = last_close > vwap
        
        if is_bullish_aligned and not price_above_vwap:
            if not (data_5m.get("bullish_choch") or data_5m.get("bullish_mss")):
                is_bullish_aligned = False
        
        if is_bearish_aligned and price_above_vwap:
            if not (data_5m.get("bearish_choch") or data_5m.get("bearish_mss")):
                is_bearish_aligned = False
    
    if not is_bullish_aligned and not is_bearish_aligned:
        return {
            "final_signal": "🟡 NEUTRAL",
            "confidence": 30.0,
            "entry": None,
            "stop_loss": None,
            "target1": None,
            "target2": None,
            "rr_ratio": None,
            "scores": {
                "5m": 50,
                "15m": 50,
                "1h": 50,
                "pressure": 50,
                "options": 50,
            },
            "signal_reason": f"VWAP conflict: Price {last_close:.2f} vs VWAP {vwap:.2f if vwap else 'N/A'}",
        }
    
    ema_trend = data_5m.get("ema_trend", "NEUTRAL")
    
    if is_bullish_aligned and ema_trend == "BEARISH":
        is_bullish_aligned = False
    
    if is_bearish_aligned and ema_trend == "BULLISH":
        is_bearish_aligned = False
    
    if not is_bullish_aligned and not is_bearish_aligned:
        return {
            "final_signal": "🟡 NEUTRAL",
            "confidence": 30.0,
            "entry": None,
            "stop_loss": None,
            "target1": None,
            "target2": None,
            "rr_ratio": None,
            "scores": {
                "5m": 50,
                "15m": 50,
                "1h": 50,
                "pressure": 50,
                "options": 50,
            },
            "signal_reason": f"EMA conflict: Trend {ema_trend} vs Structure {tf_5m}",
        }
    
    confirmation_count = 0
    total_factors = 0
    
    if tf_5m == "BULLISH":
        confirmation_count += 1
    elif tf_5m == "BEARISH":
        confirmation_count -= 1
    total_factors += 1
    
    if tf_15m == "BULLISH":
        confirmation_count += 1
    elif tf_15m == "BEARISH":
        confirmation_count -= 1
    total_factors += 1
    
    if tf_1h == "BULLISH":
        confirmation_count += 1
    elif tf_1h == "BEARISH":
        confirmation_count -= 1
    total_factors += 1
    
    if pressure_buy:
        confirmation_count += 1
    elif pressure_sell:
        confirmation_count -= 1
    total_factors += 1
    
    if vwap is not None:
        if is_bullish_aligned and price_above_vwap:
            confirmation_count += 1
        elif is_bearish_aligned and not price_above_vwap:
            confirmation_count += 1
        total_factors += 1
    
    if ema_trend == "BULLISH":
        confirmation_count += 1
    elif ema_trend == "BEARISH":
        confirmation_count -= 1
    total_factors += 1
    
    has_bullish_structure = (
        data_5m.get("bullish_choch") or 
        data_5m.get("bullish_mss") or 
        data_5m.get("bullish_cisd") or
        (data_5m.get("structure_type") in ["HH/HL", "HL"])
    )
    has_bearish_structure = (
        data_5m.get("bearish_choch") or 
        data_5m.get("bearish_mss") or 
        data_5m.get("bearish_cisd") or
        (data_5m.get("structure_type") in ["LH/LL", "LH"])
    )
    
    if is_bullish_aligned and has_bullish_structure:
        confirmation_count += 1
    elif is_bearish_aligned and has_bearish_structure:
        confirmation_count += 1
    total_factors += 1
    
    rvol = data_5m.get("rvol", 1.0)
    if rvol >= 1.2:
        confirmation_count += 1
    elif rvol < 0.8:
        confirmation_count -= 1
    total_factors += 1
    
    # Direction-aware confidence: do not turn a strongly negative tally into
    # high confidence for a bullish setup (or vice versa). Count only evidence
    # that agrees with the already validated direction.
    if total_factors > 0:
        directional_tally = confirmation_count if is_bullish_aligned else -confirmation_count
        confidence = max(0.0, min(100.0, (directional_tally / total_factors) * 100.0))
    else:
        confidence = 0.0
    
    df_5m = analysis_5m.get("df")
    if df_5m is not None:
        next_bias = calculate_next_candle_bias(df_5m, analysis_5m)
    else:
        next_bias = {"bias": "NEUTRAL", "confidence": 0.0}
    
    next_bias_str = next_bias.get("bias", "NEUTRAL")
    next_bias_conf = next_bias.get("confidence", 0.0)
    
    if is_bullish_aligned and "SELL" in next_bias_str and next_bias_conf >= 70:
        return {
            "final_signal": "🟡 NEUTRAL",
            "confidence": 40.0,
            "entry": None,
            "stop_loss": None,
            "target1": None,
            "target2": None,
            "rr_ratio": None,
            "scores": {
                "5m": 70,
                "15m": 60,
                "1h": 55,
                "pressure": 70 if pressure_strong_buy else 60,
                "options": 50,
            },
            "signal_reason": f"Next Candle Bias conflict: Bullish setup but Next Bias {next_bias_str}",
        }
    
    if is_bearish_aligned and "BUY" in next_bias_str and next_bias_conf >= 70:
        return {
            "final_signal": "🟡 NEUTRAL",
            "confidence": 40.0,
            "entry": None,
            "stop_loss": None,
            "target1": None,
            "target2": None,
            "rr_ratio": None,
            "scores": {
                "5m": 30,
                "15m": 40,
                "1h": 45,
                "pressure": 30 if pressure_strong_sell else 40,
                "options": 50,
            },
            "signal_reason": f"Next Candle Bias conflict: Bearish setup but Next Bias {next_bias_str}",
        }
    
    options_bias = options_data.get("options_bias", "NEUTRAL")
    options_conflict = False
    
    if is_bullish_aligned and "BEARISH" in str(options_bias):
        options_conflict = True
    elif is_bearish_aligned and "BULLISH" in str(options_bias):
        options_conflict = True
    
    if options_conflict:
        if not (has_bullish_structure or has_bearish_structure):
            confidence = min(confidence, 50.0)
    
    composite_score = 50
    
    if is_bullish_aligned:
        if tf_5m == "BULLISH" and tf_15m == "BULLISH":
            composite_score += 20
        elif tf_5m == "BULLISH":
            composite_score += 15
        
        if tf_1h == "BULLISH":
            composite_score += 5
        
        if pressure_strong_buy:
            composite_score += 15
        elif pressure_buy:
            composite_score += 10
        
        if vwap is not None and price_above_vwap:
            composite_score += 8
        
        if has_bullish_structure:
            composite_score += 10
        
        if rvol >= 1.5:
            composite_score += 5
        elif rvol < 1.2:
            composite_score -= 3
        
        if composite_score >= 80 and pressure_strong_buy and has_bullish_structure:
            final_signal = "🟢 STRONG BUY"
            confidence = min(100, confidence + 10)
        elif composite_score >= 70 and pressure_buy:
            final_signal = "🟢 BUY"
            confidence = min(95, confidence + 5)
        else:
            final_signal = "🟡 NEUTRAL"
            confidence = min(confidence, 60)
    
    elif is_bearish_aligned:
        if tf_5m == "BEARISH" and tf_15m == "BEARISH":
            composite_score -= 20
        elif tf_5m == "BEARISH":
            composite_score -= 15
        
        if tf_1h == "BEARISH":
            composite_score -= 5
        
        if pressure_strong_sell:
            composite_score -= 15
        elif pressure_sell:
            composite_score -= 10
        
        if vwap is not None and not price_above_vwap:
            composite_score -= 8
        
        if has_bearish_structure:
            composite_score -= 10
        
        if rvol >= 1.5:
            composite_score -= 5
        elif rvol < 1.2:
            composite_score += 3
        
        if composite_score <= 20 and pressure_strong_sell and has_bearish_structure:
            final_signal = "🔴 STRONG SELL"
            confidence = min(100, confidence + 10)
        elif composite_score <= 30 and pressure_sell:
            final_signal = "🔴 SELL"
            confidence = min(95, confidence + 5)
        else:
            final_signal = "🟡 NEUTRAL"
            confidence = min(confidence, 60)
    
    else:
        final_signal = "🟡 NEUTRAL"
        confidence = 35.0
    
    reason_parts = []
    
    if "BUY" in final_signal:
        reason_parts.append(f"5M {tf_5m}")
        reason_parts.append(f"15M {tf_15m}")
        reason_parts.append(f"1H {tf_1h}")
        if pressure_buy:
            reason_parts.append(f"Buy Pressure {data_5m.get('buying_pressure', 'N/A')}%")
        if vwap is not None and price_above_vwap:
            reason_parts.append("Above VWAP")
        if ema_trend == "BULLISH":
            reason_parts.append("EMA Bullish")
        if has_bullish_structure:
            reason_parts.append(f"{data_5m.get('structure_type', 'Structure')} Bullish")
        if rvol >= 1.5:
            reason_parts.append(f"RVOL {rvol}x")
    
    elif "SELL" in final_signal:
        reason_parts.append(f"5M {tf_5m}")
        reason_parts.append(f"15M {tf_15m}")
        reason_parts.append(f"1H {tf_1h}")
        if pressure_sell:
            reason_parts.append(f"Sell Pressure {data_5m.get('selling_pressure', 'N/A')}%")
        if vwap is not None and not price_above_vwap:
            reason_parts.append("Below VWAP")
        if ema_trend == "BEARISH":
            reason_parts.append("EMA Bearish")
        if has_bearish_structure:
            reason_parts.append(f"{data_5m.get('structure_type', 'Structure')} Bearish")
        if rvol >= 1.5:
            reason_parts.append(f"RVOL {rvol}x")
    
    else:
        reason_parts = ["Insufficient confirmation", f"TF: 5M {tf_5m} 15M {tf_15m} 1H {tf_1h}", f"Pressure: {pressure_trend}"]
    
    signal_reason = " + ".join(reason_parts)
    
    entry = sl = t1 = t2 = rr_ratio = None

    # Final safety gate: suppress actionable labels when directional evidence is weak.
    # This does not alter the individual scanner tabs' independent outputs.
    if ("BUY" in final_signal or "SELL" in final_signal) and confidence < 65:
        final_signal = "🟡 NEUTRAL"
        signal_reason = "Suppressed: directional confirmation below 65% | " + signal_reason
    
    if "BUY" in final_signal or "SELL" in final_signal:
        entry = round(last_close, 2)
        atr_5m = data_5m.get("atr", 0)
        swing_high = data_5m.get("swing_high")
        swing_low = data_5m.get("swing_low")
        
        if "BUY" in final_signal:
            sl = round(swing_low - atr_5m * 0.5, 2) if swing_low else round(entry - atr_5m * 2, 2)
            t1 = round(entry + atr_5m * 1.5, 2)
            t2 = round(entry + atr_5m * 2.5, 2)
        else:
            sl = round(swing_high + atr_5m * 0.5, 2) if swing_high else round(entry + atr_5m * 2, 2)
            t1 = round(entry - atr_5m * 1.5, 2)
            t2 = round(entry - atr_5m * 2.5, 2)
        
        risk = abs(entry - sl)
        reward = abs(t1 - entry)
        rr_ratio = round(reward / risk, 2) if risk > 0 else 0.0
    
    return {
        "final_signal": final_signal,
        "confidence": confidence,
        "entry": entry,
        "stop_loss": sl,
        "target1": t1,
        "target2": t2,
        "rr_ratio": rr_ratio,
        "scores": {
            "5m": 70 if tf_5m == "BULLISH" else 30 if tf_5m == "BEARISH" else 50,
            "15m": 70 if tf_15m == "BULLISH" else 30 if tf_15m == "BEARISH" else 50,
            "1h": 70 if tf_1h == "BULLISH" else 30 if tf_1h == "BEARISH" else 50,
            "pressure": 70 if pressure_strong_buy else 60 if pressure_buy else 30 if pressure_strong_sell else 40 if pressure_sell else 50,
            "options": 75 if "BULLISH" in str(options_bias) else 25 if "BEARISH" in str(options_bias) else 50,
        },
        "signal_reason": signal_reason,
    }

# ════════════════════════════════════════════════════════════════════════════════
# NORMALIZED SIGNAL CLASSIFICATION (for filters)
# ════════════════════════════════════════════════════════════════════════════════
def normalize_signal(signal_str: str) -> str:
    """Return normalized signal class: BUY, SELL, or NEUTRAL."""
    if pd.isna(signal_str) or signal_str is None:
        return "NEUTRAL"
    sig = str(signal_str).upper()
    if "BUY" in sig and "SELL" not in sig:
        return "BUY"
    elif "SELL" in sig:
        return "SELL"
    else:
        return "NEUTRAL"

# ════════════════════════════════════════════════════════════════════════════════
# V17 NEW FEATURES: GOLDEN CROSS / DEATH CROSS DETECTION (DAILY)
# ════════════════════════════════════════════════════════════════════════════════
def detect_golden_death_cross(fyers, symbol: str) -> Dict[str, Any]:
    """Detect Golden Cross and Death Cross using DAILY closed candles only."""
    empty = {
        "golden_cross": False,
        "death_cross": False,
        "ema50": None,
        "ema200": None,
        "ema_trend": "NEUTRAL",
        "signal": "NONE",
        "signal_date": "N/A",
        "ltp": None,
        "reason": "DATA_UNAVAILABLE"
    }
    
    try:
        date_from = (datetime.today() - timedelta(days=100)).strftime("%Y-%m-%d")
        date_to = datetime.today().strftime("%Y-%m-%d")
        
        resp, err = _safe_history(fyers, {
            "symbol": symbol,
            "resolution": "1D",
            "date_format": "1",
            "range_from": date_from,
            "range_to": date_to,
            "cont_flag": "1",
        })
        
        if err or not resp:
            empty["reason"] = "FETCH_ERROR"
            return empty
        
        candles = resp.get("candles")
        if not candles or len(candles) < 50:
            empty["reason"] = "INSUFFICIENT_DATA"
            return empty
        
        df = pd.DataFrame(candles, columns=["Time", "Open", "High", "Low", "Close", "Volume"])
        df["Time"] = pd.to_datetime(df["Time"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
        df[["Open", "High", "Low", "Close", "Volume"]] = df[["Open", "High", "Low", "Close", "Volume"]].apply(pd.to_numeric, errors="coerce")
        df = df.dropna(subset=["Close"]).sort_values("Time").reset_index(drop=True)
        
        if len(df) < 50:
            empty["reason"] = "INSUFFICIENT_DATA"
            return empty
        
        ema50 = calculate_ema(df["Close"], 50)
        ema200 = calculate_ema(df["Close"], 200)
        
        if len(ema50) < 2 or len(ema200) < 2:
            empty["reason"] = "EMA_CALCULATION_ERROR"
            return empty
        
        prev_ema50 = float(ema50.iloc[-2])
        prev_ema200 = float(ema200.iloc[-2])
        
        curr_ema50 = float(ema50.iloc[-1])
        curr_ema200 = float(ema200.iloc[-1])
        
        ltp = float(df["Close"].iloc[-1])
        signal_date = df["Time"].iloc[-1].strftime("%d-%b-%Y")
        
        if curr_ema50 > curr_ema200:
            ema_trend = "BULLISH"
        elif curr_ema50 < curr_ema200:
            ema_trend = "BEARISH"
        else:
            ema_trend = "NEUTRAL"
        
        golden_cross = (prev_ema50 <= prev_ema200) and (curr_ema50 > curr_ema200)
        death_cross = (prev_ema50 >= prev_ema200) and (curr_ema50 < curr_ema200)
        
        if golden_cross:
            signal = "🟢 GOLDEN CROSS"
        elif death_cross:
            signal = "🔴 DEATH CROSS"
        else:
            signal = "NONE"
        
        return {
            "golden_cross": golden_cross,
            "death_cross": death_cross,
            "ema50": round(curr_ema50, 2),
            "ema200": round(curr_ema200, 2),
            "ema_trend": ema_trend,
            "signal": signal,
            "signal_date": signal_date,
            "ltp": round(ltp, 2),
            "reason": "OK"
        }
    
    except Exception as e:
        empty["reason"] = f"ERROR: {str(e)}"
        return empty


# ════════════════════════════════════════════════════════════════════════════════
# SWING LONG-MOVE RADAR — DAILY TREND / MOMENTUM
# ════════════════════════════════════════════════════════════════════════════════
def detect_swing_long_move(fyers, symbol: str) -> Dict[str, Any]:
    """Educational swing radar using daily closed candles; no guaranteed forecast."""
    empty = {"symbol": symbol, "ltp": None, "ema20": None, "ema50": None, "ema200": None,
             "trend": "WAIT", "return20": None, "return60": None, "rvol": None,
             "score": 0, "status": "DATA UNAVAILABLE", "reason": "DATA_UNAVAILABLE"}
    try:
        date_from = (datetime.today() - timedelta(days=365)).strftime("%Y-%m-%d")
        date_to = datetime.today().strftime("%Y-%m-%d")
        resp, err = _safe_history(fyers, {
            "symbol": symbol, "resolution": "1D", "date_format": "1",
            "range_from": date_from, "range_to": date_to, "cont_flag": "1",
        })
        if err or not resp or not resp.get("candles"):
            return empty
        df = pd.DataFrame(resp["candles"], columns=["Time","Open","High","Low","Close","Volume"])
        df["Close"] = pd.to_numeric(df["Close"], errors="coerce")
        df["Volume"] = pd.to_numeric(df["Volume"], errors="coerce")
        df = df.dropna(subset=["Close"]).sort_values("Time").reset_index(drop=True)
        if len(df) < 210:
            empty["reason"] = "INSUFFICIENT_DATA"
            return empty
        c = df["Close"]
        e20, e50, e200 = calculate_ema(c,20), calculate_ema(c,50), calculate_ema(c,200)
        ltp = float(c.iloc[-1])
        r20 = (ltp / float(c.iloc[-21]) - 1) * 100 if len(c) > 21 else 0.0
        r60 = (ltp / float(c.iloc[-61]) - 1) * 100 if len(c) > 61 else 0.0
        vol_now = float(df["Volume"].iloc[-1]) if "Volume" in df and pd.notna(df["Volume"].iloc[-1]) else 0.0
        vol_base = float(df["Volume"].iloc[-21:-1].mean()) if len(df) > 21 else 0.0
        rvol = vol_now / vol_base if vol_base > 0 else 0.0
        score = 0
        reasons = []
        if ltp > float(e20.iloc[-1]): score += 15; reasons.append("Price>EMA20")
        if float(e20.iloc[-1]) > float(e50.iloc[-1]): score += 20; reasons.append("EMA20>EMA50")
        if float(e50.iloc[-1]) > float(e200.iloc[-1]): score += 20; reasons.append("EMA50>EMA200")
        if r20 > 3: score += 15; reasons.append("20D momentum")
        if r60 > 5: score += 15; reasons.append("60D momentum")
        if rvol >= 1.2: score += 10; reasons.append("Volume building")
        if ltp >= float(c.tail(20).max()) * 0.98: score += 5; reasons.append("Near 20D high")
        if ltp > float(e50.iloc[-1]) and float(e50.iloc[-1]) > float(e200.iloc[-1]): trend = "BULLISH"
        elif ltp < float(e50.iloc[-1]) and float(e50.iloc[-1]) < float(e200.iloc[-1]): trend = "BEARISH"
        else: trend = "NEUTRAL"
        status = "🟢 LONG-MOVE WATCH" if score >= 70 and trend == "BULLISH" else ("🟡 EARLY WATCH" if score >= 55 else "⚪ WAIT")
        return {"symbol": symbol.replace("NSE:","").replace("-EQ","").replace("-INDEX","") , "ltp": round(ltp,2),
                "ema20": round(float(e20.iloc[-1]),2), "ema50": round(float(e50.iloc[-1]),2), "ema200": round(float(e200.iloc[-1]),2),
                "trend": trend, "return20": round(r20,2), "return60": round(r60,2), "rvol": round(rvol,2),
                "score": int(min(score,100)), "status": status, "reason": " | ".join(reasons) or "No strong trend"}
    except Exception as e:
        empty["reason"] = f"ERROR: {str(e)[:120]}"
        return empty

# ════════════════════════════════════════════════════════════════════════════════
# MARKET STATISTICS
# ════════════════════════════════════════════════════════════════════════════════
def calculate_market_stats(results_df: pd.DataFrame) -> Dict[str, Any]:
    """Calculate market statistics from scan results."""
    if results_df is None or len(results_df) == 0:
        return {
            "total": 0,
            "buy": 0,
            "sell": 0,
            "neutral": 0,
            "strong_buy": 0,
            "strong_sell": 0,
            "avg_confidence": 0,
            "buy_pct": 0,
            "sell_pct": 0,
            "neutral_pct": 0,
        }
    
    try:
        total = len(results_df)
        
        normalized = results_df.get("AI SIGNAL", pd.Series([])).apply(normalize_signal)
        buy_count = len(normalized[normalized == "BUY"])
        sell_count = len(normalized[normalized == "SELL"])
        neutral_count = len(normalized[normalized == "NEUTRAL"])
        
        strong_buy = len(results_df[results_df.get("AI SIGNAL", pd.Series([])).astype(str).str.contains("STRONG BUY", na=False)])
        strong_sell = len(results_df[results_df.get("AI SIGNAL", pd.Series([])).astype(str).str.contains("STRONG SELL", na=False)])
        
        try:
            avg_conf = pd.to_numeric(results_df.get("AI CONFIDENCE %", pd.Series([])), errors='coerce').mean()
            avg_conf = 0 if pd.isna(avg_conf) else avg_conf
        except:
            avg_conf = 0
        
        buy_pct = (buy_count / total * 100) if total > 0 else 0
        sell_pct = (sell_count / total * 100) if total > 0 else 0
        neutral_pct = (neutral_count / total * 100) if total > 0 else 0
        
        return {
            "total": total,
            "buy": buy_count,
            "sell": sell_count,
            "neutral": neutral_count,
            "strong_buy": strong_buy,
            "strong_sell": strong_sell,
            "avg_confidence": round(avg_conf, 1),
            "buy_pct": round(buy_pct, 1),
            "sell_pct": round(sell_pct, 1),
            "neutral_pct": round(neutral_pct, 1),
        }
    
    except Exception as e:
        logger.error(f"Error calculating market stats: {e}")
        return {
            "total": 0,
            "buy": 0,
            "sell": 0,
            "neutral": 0,
            "strong_buy": 0,
            "strong_sell": 0,
            "avg_confidence": 0,
            "buy_pct": 0,
            "sell_pct": 0,
            "neutral_pct": 0,
        }

# ════════════════════════════════════════════════════════════════════════════════
# SAFE EXCEL EXPORT WITH FORMATTING
# ════════════════════════════════════════════════════════════════════════════════
def _safe_convert_df(df: pd.DataFrame) -> pd.DataFrame:
    """Convert all values to safe types for Excel export."""
    df_safe = df.copy()
    for col in df_safe.columns:
        try:
            df_safe[col] = df_safe[col].fillna("N/A")
            if df_safe[col].dtype in ['float64', 'float32']:
                df_safe[col] = df_safe[col].replace([np.inf, -np.inf], "N/A")
            df_safe[col] = df_safe[col].astype(str)
        except Exception:
            pass
    return df_safe

def _format_excel_output(df, scanner_type: str = "NSE") -> bytes:
    """Create professionally formatted Excel with colors and styling."""
    buf = io.BytesIO()
    
    try:
        df_export = _safe_convert_df(df)
        
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            df_export.to_excel(writer, index=False, sheet_name="Signals")
            
            workbook = writer.book
            worksheet = writer.sheets["Signals"]
            
            if OPENPYXL_AVAILABLE:
                try:
                    green_fill = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
                    red_fill = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
                    yellow_fill = PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid")
                    header_fill = PatternFill(start_color="366092", end_color="366092", fill_type="solid")
                    header_font = Font(bold=True, color="FFFFFF", size=11)
                    center_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
                    
                    for col_num in range(1, len(df_export.columns) + 1):
                        cell = worksheet.cell(row=1, column=col_num)
                        cell.fill = header_fill
                        cell.font = header_font
                        cell.alignment = center_align
                    
                    for row_num in range(2, len(df_export) + 2):
                        for col_num in range(1, len(df_export.columns) + 1):
                            cell = worksheet.cell(row=row_num, column=col_num)
                            cell.alignment = Alignment(horizontal="center", vertical="center")
                            col_title = df_export.columns[col_num - 1]
                            
                            try:
                                cell_value = str(cell.value)
                                
                                if col_title in ["AI SIGNAL", "MOMENTUM SIGNAL"]:
                                    if "BUY" in cell_value and "SELL" not in cell_value:
                                        cell.fill = green_fill
                                    elif "SELL" in cell_value:
                                        cell.fill = red_fill
                                    else:
                                        cell.fill = yellow_fill
                                
                                elif col_title in ["AI CONFIDENCE %", "MOMENTUM SCORE"]:
                                    try:
                                        conf_val = float(cell_value)
                                        if conf_val >= 70:
                                            cell.fill = green_fill
                                        elif conf_val >= 50:
                                            cell.fill = yellow_fill
                                        else:
                                            cell.fill = red_fill
                                    except:
                                        pass
                            except Exception:
                                pass
                    
                    worksheet.freeze_panes = "A2"
                    
                    for col_num, col_title in enumerate(df_export.columns, 1):
                        max_length = len(str(col_title)) + 2
                        adjusted_width = min(max_length + 2, 60)
                        col_letter = get_column_letter(col_num)
                        worksheet.column_dimensions[col_letter].width = adjusted_width
                
                except Exception as format_err:
                    logging.warning(f"Excel formatting error: {format_err}")
        
        try:
            summary_data = {
                "Metric": ["Total Stocks", "BUY Signals", "SELL Signals", "NEUTRAL Signals", "Avg Confidence %"],
                "Value": [
                    str(len(df)),
                    str(len(df[df["AI SIGNAL"].apply(lambda x: normalize_signal(x)) == "BUY"])) if "AI SIGNAL" in df.columns else "N/A",
                    str(len(df[df["AI SIGNAL"].apply(lambda x: normalize_signal(x)) == "SELL"])) if "AI SIGNAL" in df.columns else "N/A",
                    str(len(df[df["AI SIGNAL"].apply(lambda x: normalize_signal(x)) == "NEUTRAL"])) if "AI SIGNAL" in df.columns else "N/A",
                    f"{pd.to_numeric(df.get('AI CONFIDENCE %', pd.Series([])), errors='coerce').mean():.1f}%" if "AI CONFIDENCE %" in df.columns else "N/A"
                ]
            }
            summary_df = pd.DataFrame(summary_data)
            summary_df.to_excel(writer, index=False, sheet_name="Summary")
        except Exception:
            pass
    
    except Exception as e:
        logging.error(f"Excel export error: {e}")
        try:
            buf = io.BytesIO()
            df_export = _safe_convert_df(df)
            with pd.ExcelWriter(buf, engine="openpyxl") as writer:
                df_export.to_excel(writer, index=False, sheet_name="Signals")
        except Exception as final_err:
            logging.error(f"Final Excel fallback failed: {final_err}")
            buf = io.BytesIO()
            buf.write(to_csv_bytes(df))
    
    buf.seek(0)
    return buf.getvalue()

def to_csv_bytes(df) -> bytes:
    """Convert DataFrame to CSV bytes safely."""
    try:
        df_safe = _safe_convert_df(df)
        return df_safe.to_csv(index=False).encode("utf-8")
    except Exception as e:
        logging.error(f"CSV export error: {e}")
        return b"Error exporting to CSV"

def to_json_bytes(df) -> bytes:
    """Convert DataFrame to JSON bytes safely."""
    try:
        df_safe = _safe_convert_df(df)
        return df_safe.to_json(orient="records", indent=2, force_ascii=False).encode("utf-8")
    except Exception as e:
        logging.error(f"JSON export error: {e}")
        return b'{"error": "Could not export to JSON"}'

# ════════════════════════════════════════════════════════════════════════════════
# NSE SIGNAL SCANNER WORKER
# ════════════════════════════════════════════════════════════════════════════════
def _fetch_nse_signal(fyers, symbol: str):
    """Worker for NSE stocks scanning - NO options data."""
    stock_ticker = symbol.replace("NSE:", "").replace("-EQ", "") if isinstance(symbol, str) else str(symbol)
    
    if not isinstance(symbol, str) or not _VALID_EQ_SYMBOL_RE.match(symbol):
        return None, f"{symbol}: invalid format"
    
    try:
        analysis_5m = analyze_timeframe(fyers, symbol, "5")
        analysis_15m = analyze_timeframe(fyers, symbol, "15")
        analysis_1h = analyze_timeframe(fyers, symbol, "60")
        
        if all(a.get("status") != "OK" for a in [analysis_5m, analysis_15m, analysis_1h]):
            return None, None
        
        master = calculate_master_signal(symbol, analysis_5m, analysis_15m, analysis_1h)
        
        if analysis_5m.get("status") == "OK" and analysis_5m.get("data"):
            next_bias = calculate_next_candle_bias(analysis_5m.get("df"), analysis_5m)
        else:
            next_bias = {"bias": "NEUTRAL", "confidence": 0.0}
        
        ltp = None
        for analysis in [analysis_5m, analysis_15m, analysis_1h]:
            if analysis.get("status") == "OK" and analysis.get("data"):
                ltp = analysis["data"]["last_close"]
                break
        
        if ltp is None:
            return None, None
        
        data_5m = analysis_5m.get("data") if analysis_5m.get("status") == "OK" else {}
        data_15m = analysis_15m.get("data") if analysis_15m.get("status") == "OK" else {}
        data_1h = analysis_1h.get("data") if analysis_1h.get("status") == "OK" else {}
        
        return {
            "Symbol": stock_ticker,
            "LTP": round(float(ltp), 2),
            "Trend": data_5m.get("structure_trend", "N/A"),
            "5M Trend": data_5m.get("structure_trend", "N/A"),
            "15M Trend": data_15m.get("structure_trend", "N/A"),
            "1H Trend": data_1h.get("structure_trend", "N/A"),
            "5M Structure": data_5m.get("structure_type", "N/A"),
            "15M Structure": data_15m.get("structure_type", "N/A"),
            "1H Structure": data_1h.get("structure_type", "N/A"),
            "5M BOS": "✅" if data_5m.get("bullish_choch") else "❌" if data_5m.get("bearish_choch") else "−",
            "15M BOS": "✅" if data_15m.get("bullish_choch") else "❌" if data_15m.get("bearish_choch") else "−",
            "1H BOS": "✅" if data_1h.get("bullish_choch") else "❌" if data_1h.get("bearish_choch") else "−",
            "5M CHoCH": "✅" if data_5m.get("bullish_choch") else "❌" if data_5m.get("bearish_choch") else "−",
            "15M CHoCH": "✅" if data_15m.get("bullish_choch") else "❌" if data_15m.get("bearish_choch") else "−",
            "1H CHoCH": "✅" if data_1h.get("bullish_choch") else "❌" if data_1h.get("bearish_choch") else "−",
            "5M MSS": "✅" if data_5m.get("bullish_mss") else "❌" if data_5m.get("bearish_mss") else "−",
            "15M MSS": "✅" if data_15m.get("bullish_mss") else "❌" if data_15m.get("bearish_mss") else "−",
            "1H MSS": "✅" if data_1h.get("bullish_mss") else "❌" if data_1h.get("bearish_mss") else "−",
            "5M CISD": "✅" if data_5m.get("bullish_cisd") else "❌" if data_5m.get("bearish_cisd") else "−",
            "VWAP": round(data_5m.get("vwap", 0), 2) if data_5m.get("vwap") else "N/A",
            "RSI": round(data_5m.get("rsi", 50), 1),
            "MACD": "🟢" if data_5m.get("macd_bullish") else "🔴",
            "ATR": data_5m.get("atr", "N/A"),
            "Volume": data_5m.get("rvol", "N/A"),
            "RVOL": data_5m.get("rvol", 0),
            "🟢 BUY PRESSURE %": data_5m.get("buying_pressure", "N/A"),
            "🔴 SELL PRESSURE %": data_5m.get("selling_pressure", "N/A"),
            "PRESSURE SIGNAL": data_5m.get("pressure_trend", "N/A"),
            "EST. BUY VOLUME (20x5M)": data_5m.get("estimated_buy_volume", 0),
            "EST. SELL VOLUME (20x5M)": data_5m.get("estimated_sell_volume", 0),
            "VOLUME DELTA (20x5M)": data_5m.get("volume_delta", 0),
            "DELTA % (20x5M)": data_5m.get("delta_pct", 0),
            "CUMULATIVE DELTA": data_5m.get("cumulative_delta", 0),
            "LAST 5M DELTA": data_5m.get("last_candle_delta", 0),
            "ORDER FLOW SIGNAL": data_5m.get("order_flow_signal", "WAIT"),
            "DELTA DIVERGENCE": data_5m.get("delta_divergence", "NONE"),
            "15M ORDER FLOW": data_15m.get("order_flow_signal", "N/A"),
            "15M DELTA %": data_15m.get("delta_pct", "N/A"),
            "1H ORDER FLOW": data_1h.get("order_flow_signal", "N/A"),
            "1H DELTA %": data_1h.get("delta_pct", "N/A"),
            "NEXT CANDLE BIAS": next_bias.get("bias", "NEUTRAL"),
            "NEXT CANDLE CONFIDENCE %": next_bias.get("confidence", 0.0),
            "AI SIGNAL": master["final_signal"],
            "AI CONFIDENCE %": master["confidence"],
            "SIGNAL REASON": master["signal_reason"],
            "ENTRY": master["entry"],
            "STOP LOSS": master["stop_loss"],
            "TARGET 1": master["target1"],
            "TARGET 2": master["target2"],
            "RISK:REWARD": master["rr_ratio"],
        }, None
    
    except Exception as e:
        return None, f"{symbol}: error ({type(e).__name__})"

# ════════════════════════════════════════════════════════════════════════════════
# F&O SIGNAL SCANNER WORKER
# ════════════════════════════════════════════════════════════════════════════════
def _fetch_fo_signal(fyers, symbol: str):
    """Worker for F&O stocks scanning - WITH options data."""
    stock_ticker = symbol.replace("NSE:", "").replace("-EQ", "") if isinstance(symbol, str) else str(symbol)
    
    if not isinstance(symbol, str) or not _VALID_EQ_SYMBOL_RE.match(symbol):
        return None, f"{symbol}: invalid format"
    
    try:
        analysis_5m = analyze_timeframe(fyers, symbol, "5")
        analysis_15m = analyze_timeframe(fyers, symbol, "15")
        analysis_1h = analyze_timeframe(fyers, symbol, "60")
        
        if all(a.get("status") != "OK" for a in [analysis_5m, analysis_15m, analysis_1h]):
            return None, None
        
        options_data = fetch_options_chain_data(fyers, symbol)
        
        master = calculate_master_signal(symbol, analysis_5m, analysis_15m, analysis_1h, options_data)
        
        if analysis_5m.get("status") == "OK" and analysis_5m.get("data"):
            next_bias = calculate_next_candle_bias(analysis_5m.get("df"), analysis_5m)
        else:
            next_bias = {"bias": "NEUTRAL", "confidence": 0.0}
        
        ltp = None
        for analysis in [analysis_5m, analysis_15m, analysis_1h]:
            if analysis.get("status") == "OK" and analysis.get("data"):
                ltp = analysis["data"]["last_close"]
                break
        
        if ltp is None:
            return None, None
        
        data_5m = analysis_5m.get("data") if analysis_5m.get("status") == "OK" else {}
        data_15m = analysis_15m.get("data") if analysis_15m.get("status") == "OK" else {}
        data_1h = analysis_1h.get("data") if analysis_1h.get("status") == "OK" else {}
        
        return {
            "Symbol": stock_ticker,
            "LTP": round(float(ltp), 2),
            "Trend": data_5m.get("structure_trend", "N/A"),
            "5M Trend": data_5m.get("structure_trend", "N/A"),
            "15M Trend": data_15m.get("structure_trend", "N/A"),
            "1H Trend": data_1h.get("structure_trend", "N/A"),
            "5M Structure": data_5m.get("structure_type", "N/A"),
            "15M Structure": data_15m.get("structure_type", "N/A"),
            "1H Structure": data_1h.get("structure_type", "N/A"),
            "5M BOS": "✅" if data_5m.get("bullish_choch") else "❌" if data_5m.get("bearish_choch") else "−",
            "15M BOS": "✅" if data_15m.get("bullish_choch") else "❌" if data_15m.get("bearish_choch") else "−",
            "1H BOS": "✅" if data_1h.get("bullish_choch") else "❌" if data_1h.get("bearish_choch") else "−",
            "5M CHoCH": "✅" if data_5m.get("bullish_choch") else "❌" if data_5m.get("bearish_choch") else "−",
            "15M CHoCH": "✅" if data_15m.get("bullish_choch") else "❌" if data_15m.get("bearish_choch") else "−",
            "1H CHoCH": "✅" if data_1h.get("bullish_choch") else "❌" if data_1h.get("bearish_choch") else "−",
            "5M MSS": "✅" if data_5m.get("bullish_mss") else "❌" if data_5m.get("bearish_mss") else "−",
            "15M MSS": "✅" if data_15m.get("bullish_mss") else "❌" if data_15m.get("bearish_mss") else "−",
            "1H MSS": "✅" if data_1h.get("bullish_mss") else "❌" if data_1h.get("bearish_mss") else "−",
            "5M CISD": "✅" if data_5m.get("bullish_cisd") else "❌" if data_5m.get("bearish_cisd") else "−",
            "VWAP": round(data_5m.get("vwap", 0), 2) if data_5m.get("vwap") else "N/A",
            "RSI": round(data_5m.get("rsi", 50), 1),
            "MACD": "🟢" if data_5m.get("macd_bullish") else "🔴",
            "ATR": data_5m.get("atr", "N/A"),
            "Volume": data_5m.get("rvol", "N/A"),
            "RVOL": data_5m.get("rvol", 0),
            "🟢 BUY PRESSURE %": data_5m.get("buying_pressure", "N/A"),
            "🔴 SELL PRESSURE %": data_5m.get("selling_pressure", "N/A"),
            "PRESSURE SIGNAL": data_5m.get("pressure_trend", "N/A"),
            "EST. BUY VOLUME (20x5M)": data_5m.get("estimated_buy_volume", 0),
            "EST. SELL VOLUME (20x5M)": data_5m.get("estimated_sell_volume", 0),
            "VOLUME DELTA (20x5M)": data_5m.get("volume_delta", 0),
            "DELTA % (20x5M)": data_5m.get("delta_pct", 0),
            "CUMULATIVE DELTA": data_5m.get("cumulative_delta", 0),
            "LAST 5M DELTA": data_5m.get("last_candle_delta", 0),
            "ORDER FLOW SIGNAL": data_5m.get("order_flow_signal", "WAIT"),
            "DELTA DIVERGENCE": data_5m.get("delta_divergence", "NONE"),
            "15M ORDER FLOW": data_15m.get("order_flow_signal", "N/A"),
            "15M DELTA %": data_15m.get("delta_pct", "N/A"),
            "1H ORDER FLOW": data_1h.get("order_flow_signal", "N/A"),
            "1H DELTA %": data_1h.get("delta_pct", "N/A"),
            "NEXT CANDLE BIAS": next_bias.get("bias", "NEUTRAL"),
            "NEXT CANDLE CONFIDENCE %": next_bias.get("confidence", 0.0),
            "ATM STRIKE": round(options_data.get("atm_strike", 0), 2) if options_data.get("atm_strike") else "N/A",
            "PCR": options_data.get("pcr", "N/A"),
            "CE OI": options_data.get("ce_oi", "N/A"),
            "PE OI": options_data.get("pe_oi", "N/A"),
            "CE OI CHANGE": options_data.get("ce_oi_change", "N/A"),
            "PE OI CHANGE": options_data.get("pe_oi_change", "N/A"),
            "OPTIONS BIAS": options_data.get("options_bias", "N/A"),
            "AI SIGNAL": master["final_signal"],
            "AI CONFIDENCE %": master["confidence"],
            "SIGNAL REASON": master["signal_reason"],
            "ENTRY": master["entry"],
            "STOP LOSS": master["stop_loss"],
            "TARGET 1": master["target1"],
            "TARGET 2": master["target2"],
            "RISK:REWARD": master["rr_ratio"],
        }, None
    
    except Exception as e:
        return None, f"{symbol}: error ({type(e).__name__})"

# ════════════════════════════════════════════════════════════════════════════════
# MOMENTUM SCANNER WORKER (NEW)
# ════════════════════════════════════════════════════════════════════════════════
def detect_block_order_activity(df: pd.DataFrame) -> Dict[str, Any]:
    """Estimate institutional/block-order activity from OHLCV only.

    This is a probability score, NOT a true exchange order-book/block-trade feed.
    It uses abnormal volume, candle body/wicks, price acceptance/rejection and
    repeated high-volume levels to flag possible large-player activity.
    """
    out = {
        "block_score": 0.0,
        "block_signal": "NONE",
        "block_side": "NONE",
        "block_level": None,
        "block_rvol": 0.0,
        "block_reason": "Insufficient data",
    }
    if df is None or len(df) < 25:
        return out
    try:
        d = df.reset_index(drop=True).copy()
        cols = ["Open", "High", "Low", "Close", "Volume"]
        if any(c not in d.columns for c in cols):
            out["block_reason"] = "Missing OHLCV columns"
            return out

        last = d.iloc[-1]
        o,h,l,c,v = [float(last[x]) for x in cols]
        prev = float(d["Close"].iloc[-2])
        if min(o,h,l,c,prev) <= 0:
            return out

        rng = max(h-l, 1e-9)
        body = abs(c-o)
        body_pct = body/rng*100
        upper_wick = h-max(o,c)
        lower_wick = min(o,c)-l
        close_pos = (c-l)/rng

        base = float(d["Volume"].iloc[-21:-1].mean())
        rvol = v/base if base > 0 else 0.0
        recent = d.tail(20).copy()
        avg_ranges = (recent["High"]-recent["Low"]).astype(float).replace(0,np.nan).mean()
        range_ratio = rng/float(avg_ranges) if avg_ranges and pd.notna(avg_ranges) else 0.0

        # High-volume candle close behavior.
        buy_points = sell_points = 0.0
        reasons=[]
        if rvol >= 2.0:
            buy_points += 20; sell_points += 20; reasons.append(f"RVOL {rvol:.1f}x")
        elif rvol >= 1.5:
            buy_points += 12; sell_points += 12; reasons.append(f"RVOL {rvol:.1f}x")
        if range_ratio >= 1.5:
            buy_points += 10; sell_points += 10; reasons.append("large range")

        # Acceptance near high = possible buy absorption/accumulation.
        if close_pos >= 0.75 and c > o:
            buy_points += 25
        if close_pos <= 0.25 and c < o:
            sell_points += 25

        # Rejection/absorption: large wick with high volume.
        if lower_wick/rng >= 0.45 and rvol >= 1.5:
            buy_points += 20; reasons.append("lower-wick absorption")
        if upper_wick/rng >= 0.45 and rvol >= 1.5:
            sell_points += 20; reasons.append("upper-wick rejection")

        # Repeated volume concentration around current price.
        lo, hi = float(recent["Low"].min()), float(recent["High"].max())
        if hi > lo:
            bins = np.linspace(lo, hi, 11)
            mids=(bins[:-1]+bins[1:])/2
            idx=np.clip(np.digitize(recent["Close"].astype(float), bins)-1,0,9)
            vol_by_bin=recent.groupby(idx)["Volume"].sum()
            if len(vol_by_bin):
                peak_bin=int(vol_by_bin.idxmax())
                peak_level=float(mids[peak_bin])
                peak_share=float(vol_by_bin.max()/max(float(recent["Volume"].sum()),1.0))
                if peak_share >= 0.20 and abs(c-peak_level)/c <= 0.02:
                    buy_points += 15 if c >= peak_level else 5
                    sell_points += 15 if c <= peak_level else 5
                    reasons.append("volume concentration")
                else:
                    peak_level=c
        else:
            peak_level=c

        # Directional confirmation from recent closes.
        ret3 = (c/float(d["Close"].iloc[-4])-1)*100 if len(d)>=4 else 0
        if ret3 > 0.4: buy_points += 10
        elif ret3 < -0.4: sell_points += 10

        buy_points=min(100,buy_points); sell_points=min(100,sell_points)
        side = "BUY" if buy_points > sell_points else "SELL" if sell_points > buy_points else "NONE"
        score=max(buy_points,sell_points)
        if score >= 75:
            signal="🔥 VERY HIGH"
        elif score >= 60:
            signal="🟢 HIGH BUY" if side=="BUY" else "🔴 HIGH SELL" if side=="SELL" else "🟡 HIGH"
        elif score >= 45:
            signal="🟡 POSSIBLE"
        else:
            signal="NONE"

        direction_reason = "BUY-side" if side=="BUY" else "SELL-side" if side=="SELL" else "mixed"
        if signal == "NONE":
            side = "NONE"
            peak_level = None
            rvol = 0.0
            direction_reason = "no confirmed block activity"

        out.update({
            "block_score": round(score,1),
            "block_signal": signal,
            "block_side": side,
            "block_level": round(float(peak_level),2) if peak_level is not None else None,
            "block_rvol": round(rvol,2),
            "block_reason": f"{direction_reason} | " + (" + ".join(reasons) if reasons else "normal volume/price behavior"),
        })
        return out
    except Exception as e:
        out["block_reason"] = f"ERROR: {str(e)[:100]}"
        return out


def _pre_move_signal(df: pd.DataFrame, block: Dict[str, Any] = None) -> Dict[str, Any]:
    """Enhanced PRE-MOVE engine.

    Keeps the existing PRE-MOVE columns/statuses, but strengthens the early-warning
    decision using volume buildup, price compression, breakout proximity, VWAP
    pressure, short-term pressure, market structure and optional block activity.
    PRE-MOVE is an early-warning model, not a guaranteed future-price prediction.
    """
    out = {
        "status": "⚪ WAIT", "direction": "NONE", "score": 0.0,
        "buy_score": 0.0, "sell_score": 0.0, "score_gap": 0.0,
        "reason": "Insufficient setup", "breakout_level": None,
        "breakdown_level": None, "rvol": 0.0,
    }
    if df is None or len(df) < 30:
        return out
    try:
        d = df.reset_index(drop=True).copy()
        required = ["Open", "High", "Low", "Close", "Volume"]
        if any(c not in d.columns for c in required):
            out["reason"] = "Missing OHLCV columns"
            return out

        for c in required:
            d[c] = pd.to_numeric(d[c], errors="coerce")
        d = d.dropna(subset=required).reset_index(drop=True)
        if len(d) < 30:
            return out

        last = d.iloc[-1]
        close = float(last["Close"])
        open_ = float(last["Open"])
        high = float(last["High"])
        low = float(last["Low"])
        if close <= 0:
            out["reason"] = "Invalid price data"
            return out

        atr = max(float(_last_valid_atr(d, 14) or 0), close * 0.001)

        # ------------------------- RANGE / COMPRESSION -------------------------
        # IMPORTANT: breakout/breakdown reference must come from the PRIOR
        # completed range. Including the current candle makes BREAKOUT LEVEL
        # frequently equal to LTP and can create a false PRE-MOVE signal.
        recent = d.iloc[-11:-1] if len(d) >= 11 else d.iloc[:-1]
        if len(recent) < 5:
            return out
        rh = float(recent["High"].max())
        rl = float(recent["Low"].min())
        rsize = max(rh - rl, 1e-9)
        compression = (rsize / atr) <= 3.0

        # Compare the latest range with the preceding range. Smaller recent
        # ranges indicate energy being compressed before a possible expansion.
        prev10 = d.iloc[-20:-10]
        prev_range = float(prev10["High"].max() - prev10["Low"].min()) if len(prev10) else rsize
        tight_compression = compression and rsize <= max(prev_range * 0.85, atr * 1.5)

        # ---------------------------- VOLUME BUILDUP ----------------------------
        base = d["Volume"].iloc[-30:-5].astype(float)
        base_vol = float(base.mean()) if len(base) else 0.0
        recent_vol_series = d["Volume"].iloc[-5:].astype(float)
        recent_vol = float(recent_vol_series.mean()) if len(recent_vol_series) else 0.0
        last_vol = float(last["Volume"])
        rvol = last_vol / base_vol if base_vol > 0 else 0.0
        recent_rvol = recent_vol / base_vol if base_vol > 0 else 0.0
        volume_building = base_vol > 0 and recent_vol >= base_vol * 1.15
        strong_volume_building = base_vol > 0 and recent_vol >= base_vol * 1.35

        # Volume acceleration: compare the latest 2 candles with the prior 3.
        v_early = float(d["Volume"].iloc[-5:-2].mean()) if len(d) >= 5 else 0.0
        v_late = float(d["Volume"].iloc[-2:].mean()) if len(d) >= 2 else 0.0
        volume_accel = (v_late / v_early) if v_early > 0 else 1.0

        # -------------------------- BREAKOUT PROXIMITY --------------------------
        pos = (close - rl) / rsize
        near_high = pos >= 0.70
        near_low = pos <= 0.30
        very_near_high = pos >= 0.88
        very_near_low = pos <= 0.12
        breakout_distance_pct = ((rh - close) / close) * 100.0
        breakdown_distance_pct = ((close - rl) / close) * 100.0

        # --------------------------- PRICE PRESSURE ----------------------------
        ret5 = ((close / float(d["Close"].iloc[-6])) - 1.0) * 100.0
        ret3 = ((close / float(d["Close"].iloc[-4])) - 1.0) * 100.0

        # Candle-body pressure across recent candles. This is intentionally a
        # simple OHLCV proxy, not a true exchange order-flow measurement.
        recent20 = d.iloc[-20:]
        up_vol = float(recent20.loc[recent20["Close"] > recent20["Open"], "Volume"].sum())
        down_vol = float(recent20.loc[recent20["Close"] < recent20["Open"], "Volume"].sum())
        eq_vol = float(recent20.loc[recent20["Close"] == recent20["Open"], "Volume"].sum())
        total_dir_vol = up_vol + down_vol + eq_vol
        buy_pressure = ((up_vol + eq_vol * 0.5) / total_dir_vol * 100.0) if total_dir_vol > 0 else 50.0
        sell_pressure = 100.0 - buy_pressure

        # Latest candle location in its own range.
        candle_range = max(high - low, 1e-9)
        close_location = (close - low) / candle_range

        # ----------------------------- STRUCTURE -------------------------------
        ph, pl = _confirmed_pivots(d.tail(30), left=1, right=1)
        hh_hl = len(ph) >= 2 and len(pl) >= 2 and ph[-1][1] > ph[-2][1] and pl[-1][1] > pl[-2][1]
        lh_ll = len(ph) >= 2 and len(pl) >= 2 and ph[-1][1] < ph[-2][1] and pl[-1][1] < pl[-2][1]

        # ------------------------------- VWAP ---------------------------------
        typical = (d["High"] + d["Low"] + d["Close"]) / 3.0
        vol_sum = d["Volume"].replace(0, pd.NA).cumsum()
        pv = typical * d["Volume"]
        cum_pv = pv.cumsum()
        vwap_series = cum_pv / vol_sum
        vwap = float(vwap_series.iloc[-1]) if pd.notna(vwap_series.iloc[-1]) else close
        vwap_pct = ((close - vwap) / vwap) * 100.0 if vwap > 0 else 0.0
        above_vwap = close > vwap * 1.0002
        below_vwap = close < vwap * 0.9998

        # --------------------------- SCORING -----------------------------------
        buy = sell = 0.0
        br, sr = [], []

        # Neutral energy factors are added to both sides because they identify
        # a potential expansion but do not predict its direction.
        if compression:
            buy += 15; sell += 15
            br.append("Compression"); sr.append("Compression")
        if tight_compression:
            buy += 8; sell += 8
            br.append("Tight Range"); sr.append("Tight Range")

        if volume_building:
            buy += 12; sell += 12
            br.append(f"Volume Building {recent_rvol:.2f}x"); sr.append(f"Volume Building {recent_rvol:.2f}x")
        if strong_volume_building:
            buy += 8; sell += 8
        if rvol >= 1.50:
            buy += 8; sell += 8
        if volume_accel >= 1.25:
            buy += 5; sell += 5

        # Directional breakout proximity.
        if very_near_high:
            buy += 22; br.append(f"Near Breakout {breakout_distance_pct:.2f}%")
        elif near_high:
            buy += 15; br.append("Near Breakout")
        if very_near_low:
            sell += 22; sr.append(f"Near Breakdown {breakdown_distance_pct:.2f}%")
        elif near_low:
            sell += 15; sr.append("Near Breakdown")

        # Short-term price pressure.
        if ret5 > 0.10:
            buy += 8; br.append(f"5C Buy Pressure {ret5:+.2f}%")
        elif ret5 < -0.10:
            sell += 8; sr.append(f"5C Sell Pressure {ret5:+.2f}%")
        if ret3 > 0.05:
            buy += 5
        elif ret3 < -0.05:
            sell += 5

        # OHLCV volume pressure.
        if buy_pressure >= 65:
            buy += 15; br.append(f"Buy Vol {buy_pressure:.0f}%")
        elif buy_pressure >= 55:
            buy += 8; br.append(f"Buy Vol {buy_pressure:.0f}%")
        if sell_pressure >= 65:
            sell += 15; sr.append(f"Sell Vol {sell_pressure:.0f}%")
        elif sell_pressure >= 55:
            sell += 8; sr.append(f"Sell Vol {sell_pressure:.0f}%")

        # Latest candle closing location gives a small confirmation of pressure.
        if close_location >= 0.75:
            buy += 6; br.append("Strong Close")
        elif close_location <= 0.25:
            sell += 6; sr.append("Weak Close")

        # Structure.
        if hh_hl:
            buy += 18; br.append("HH/HL")
        if lh_ll:
            sell += 18; sr.append("LH/LL")

        # VWAP directional filter. Do not over-weight it because VWAP alone is
        # not a breakout signal.
        if above_vwap and (ret3 > 0 or near_high):
            buy += 8; br.append(f"Above VWAP {vwap_pct:+.2f}%")
        elif below_vwap and (ret3 < 0 or near_low):
            sell += 8; sr.append(f"Below VWAP {vwap_pct:+.2f}%")

        # Optional block/order activity already calculated by the existing app.
        if block:
            bsig = str(block.get("block_signal", "NONE")).upper()
            bside = str(block.get("block_side", "NONE")).upper()
            bscore = float(block.get("block_score", 0) or 0)
            if bsig != "NONE" and bscore >= 40:
                if bside == "BUY":
                    buy += 15; br.append("Possible Buy Block")
                elif bside == "SELL":
                    sell += 15; sr.append("Possible Sell Block")

        buy, sell = min(buy, 100.0), min(sell, 100.0)
        score, gap = max(buy, sell), abs(buy - sell)
        direction, status, reasons = "NONE", "⚪ WAIT", ["No clear directional setup"]

        # Stronger direction filter: a side must lead by a meaningful margin.
        if score >= 55 and gap >= 10:
            direction = "BUY" if buy > sell else "SELL"
            reasons = br if direction == "BUY" else sr
            if not reasons:
                reasons = ["Directional pressure detected"]

            # Existing status names are preserved so the current UI/order logic
            # continues to work without requiring another PRE-MOVE column.
            if score >= 85 and gap >= 20:
                status = "🚀 PRE-BIG MOVE UP READY" if direction == "BUY" else "🚨 PRE-BIG MOVE DOWN READY"
            elif score >= 75 and gap >= 15:
                status = "🟢 UP MOVE BUILDING" if direction == "BUY" else "🔴 DOWN MOVE BUILDING"
            else:
                status = "🟡 POSSIBLE UP MOVE" if direction == "BUY" else "🟡 POSSIBLE DOWN MOVE"

        out.update({
            "status": status, "direction": direction, "score": round(score, 1),
            "buy_score": round(buy, 1), "sell_score": round(sell, 1),
            "score_gap": round(gap, 1), "reason": " + ".join(reasons),
            "breakout_level": round(rh, 2), "breakdown_level": round(rl, 2),
            "rvol": round(rvol, 2),
        })
        return out
    except Exception as e:
        out["reason"] = f"PRE MOVE error: {type(e).__name__}"
        return out


def _before_move_signal(df: pd.DataFrame, pre: Dict[str, Any] = None) -> Dict[str, Any]:
    """Dedicated early-warning signal; existing PRE-MOVE/live signals stay unchanged."""
    out = {"signal":"🟡 WAIT","direction":"NONE","score":0.0,"reason":"No high-quality setup","trigger":""}
    try:
        if df is None or len(df) < 30: return out
        d=df.reset_index(drop=True).copy()
        for c in ["Open","High","Low","Close","Volume"]:
            if c not in d.columns: return out
            d[c]=pd.to_numeric(d[c],errors="coerce")
        d=d.dropna(subset=["Open","High","Low","Close","Volume"]).reset_index(drop=True)
        if len(d)<30: return out
        p=pre if isinstance(pre,dict) else _pre_move_signal(d)
        buy=float(p.get("buy_score",0) or 0); sell=float(p.get("sell_score",0) or 0)
        gap=abs(buy-sell); direction="BUY" if buy>sell else "SELL" if sell>buy else "NONE"
        score=max(buy,sell); close=float(d["Close"].iloc[-1]); high=float(d["High"].iloc[-1]); low=float(d["Low"].iloc[-1])
        atr_s=calculate_atr(d,14); atr=float(atr_s.iloc[-1]) if len(atr_s) and pd.notna(atr_s.iloc[-1]) else max(close*0.003,0.01)
        # Use the prior completed 10-candle range as the trigger reference.
        ref=d.iloc[-11:-1] if len(d)>=11 else d.iloc[:-1]
        if len(ref)<5: return out
        rh=float(ref["High"].max()); rl=float(ref["Low"].min()); rng=max(rh-rl,1e-9); pos=(close-rl)/rng
        # BEFORE MOVE is only an early-warning state. Once price has already
        # broken the reference level, do not call it BEFORE MOVE.
        already_breakout = close >= rh
        already_breakdown = close <= rl
        cr=max(high-low,1e-9); close_loc=(close-low)/cr
        prev_rng=float(d["High"].iloc[-20:-10].max()-d["Low"].iloc[-20:-10].min())
        compression=rng<=atr*3.0; tightening=compression and rng<=max(prev_rng*0.90,atr*1.6)
        base_vol=float(d["Volume"].iloc[-30:-5].mean()); recent_vol=float(d["Volume"].iloc[-5:].mean())
        vol_build=base_vol>0 and recent_vol>=base_vol*1.12
        early=float(d["Volume"].iloc[-5:-2].mean()); late=float(d["Volume"].iloc[-2:].mean()); vol_accel=late/early if early>0 else 1.0
        ret3=((close/float(d["Close"].iloc[-4]))-1)*100
        typical=(d["High"]+d["Low"]+d["Close"])/3; vwap_s=(typical*d["Volume"]).cumsum()/d["Volume"].replace(0,pd.NA).cumsum(); vwap=float(vwap_s.iloc[-1]) if pd.notna(vwap_s.iloc[-1]) else close
        above=close>vwap*1.0002; below=close<vwap*0.9998
        ph,pl=_confirmed_pivots(d.tail(30),left=1,right=1); hh_hl=len(ph)>=2 and len(pl)>=2 and ph[-1][1]>ph[-2][1] and pl[-1][1]>pl[-2][1]; lh_ll=len(ph)>=2 and len(pl)>=2 and ph[-1][1]<ph[-2][1] and pl[-1][1]<pl[-2][1]
        reasons=[]
        if compression: score+=5; reasons.append("Compression")
        if tightening: score+=5; reasons.append("Tightening")
        if vol_build: score+=5; reasons.append("Volume Build")
        if vol_accel>=1.15: score+=4; reasons.append("Volume Acceleration")
        if direction=="BUY":
            if pos>=0.72: score+=7; reasons.append("Near Breakout")
            if ret3>0: score+=4; reasons.append("Short-term Up")
            if above: score+=4; reasons.append("Above VWAP")
            if close_loc>=0.65: score+=3; reasons.append("Strong Close")
            if hh_hl: score+=5; reasons.append("HH/HL")
        elif direction=="SELL":
            if pos<=0.28: score+=7; reasons.append("Near Breakdown")
            if ret3<0: score+=4; reasons.append("Short-term Down")
            if below: score+=4; reasons.append("Below VWAP")
            if close_loc<=0.35: score+=3; reasons.append("Weak Close")
            if lh_ll: score+=5; reasons.append("LH/LL")
        score=min(100.0,score)
        if direction=="BUY" and score>=72 and gap>=12 and (compression or vol_build) and not already_breakout:
            out.update(signal="🟢 BUY BEFORE MOVE",direction="BUY",trigger=f"Breakout > {rh:.2f}")
        elif direction=="SELL" and score>=72 and gap>=12 and (compression or vol_build) and not already_breakdown:
            out.update(signal="🔴 SELL BEFORE MOVE",direction="SELL",trigger=f"Breakdown < {rl:.2f}")
        elif already_breakout or already_breakdown:
            out["reason"] = "Breakout/breakdown already triggered; not a BEFORE MOVE setup"
        out["score"]=round(score,1); out["reason"]=" + ".join(reasons[:7]) if reasons else "Waiting for directional energy"
        return out
    except Exception as e:
        out["reason"]=f"BEFORE MOVE error: {type(e).__name__}"; return out


def _intraday_pre_move_confirmation(df5: pd.DataFrame, df15: pd.DataFrame = None) -> Dict[str, Any]:
    """Additive 5M sweep->reversal->breakout->retest confirmation engine.

    Requires completed candles. It is a rule-based setup detector, not an AI
    probability estimate. Existing scanner engines are intentionally untouched.
    """
    out = {
        "Stage": "NO SETUP", "Final Signal": "WAIT", "Direction": "NONE",
        "Setup Score": 0, "Sweep Time": "N/A", "Breakout Level": None,
        "Entry": None, "Stop Loss": None, "Target 1.5R": None,
        "RVOL": 0.0, "Reason": "Waiting for valid sweep/reversal sequence",
    }
    try:
        required = ["Open", "High", "Low", "Close", "Volume"]
        if df5 is None or len(df5) < 35 or any(c not in df5.columns for c in required):
            out["Reason"] = "Insufficient 5M OHLCV data"
            return out
        d = df5.copy().reset_index(drop=True)
        for c in required:
            d[c] = pd.to_numeric(d[c], errors="coerce")
        d = d.dropna(subset=required).reset_index(drop=True)
        if len(d) < 35:
            out["Reason"] = "Insufficient clean 5M candles"
            return out
        # Use the last completed candle and search recent history for an ordered setup.
        atr_s = calculate_atr(d, 14)
        atr = float(atr_s.iloc[-1]) if len(atr_s) and pd.notna(atr_s.iloc[-1]) else float(d["Close"].iloc[-1]) * .003
        atr = max(atr, 1e-9)
        vol_base = float(d["Volume"].iloc[-21:-1].mean())
        rvol = float(d["Volume"].iloc[-1]) / vol_base if vol_base > 0 else 0.0
        out["RVOL"] = round(rvol, 2)

        # Higher-timeframe trend filter; if unavailable, do not invent one.
        trend = "UNKNOWN"
        if df15 is not None and len(df15) >= 25 and "Close" in df15.columns:
            c15 = pd.to_numeric(df15["Close"], errors="coerce").dropna()
            if len(c15) >= 25:
                ema20 = c15.ewm(span=20, adjust=False).mean().iloc[-1]
                ema50 = c15.ewm(span=50, adjust=False).mean().iloc[-1]
                if c15.iloc[-1] > ema20 and ema20 > ema50:
                    trend = "UP"
                elif c15.iloc[-1] < ema20 and ema20 < ema50:
                    trend = "DOWN"
        out["15M Trend"] = trend

        # Look for a sweep in the latest 8 completed bars, using only bars before it
        # to define the liquidity level (prevents using the sweep candle in its level).
        n = len(d)
        candidate = None
        start = max(20, n - 9)
        for i in range(start, n - 2):
            prior = d.iloc[max(0, i - 12):i]
            if len(prior) < 8:
                continue
            row = d.iloc[i]
            level_low = float(prior["Low"].min())
            level_high = float(prior["High"].max())
            rng = max(float(row["High"] - row["Low"]), 1e-9)
            body = abs(float(row["Close"] - row["Open"]))
            lower_wick = min(float(row["Open"]), float(row["Close"])) - float(row["Low"])
            upper_wick = float(row["High"]) - max(float(row["Open"]), float(row["Close"]))
            bull_sweep = float(row["Low"]) < level_low and float(row["Close"]) > level_low and lower_wick >= max(body * 0.8, rng * 0.20)
            bear_sweep = float(row["High"]) > level_high and float(row["Close"]) < level_high and upper_wick >= max(body * 0.8, rng * 0.20)
            if bull_sweep or bear_sweep:
                candidate = {"i": i, "direction": "BUY" if bull_sweep else "SELL",
                             "level": level_low if bull_sweep else level_high,
                             "extreme": float(row["Low"] if bull_sweep else row["High"])}
        if candidate is None:
            out["Reason"] = "No recent confirmed liquidity sweep"
            return out

        si = candidate["i"]
        direction = candidate["direction"]
        level = float(candidate["level"])
        extreme = float(candidate["extreme"])
        sweep_time = str(d.iloc[si]["Time"]) if "Time" in d.columns else str(si)
        out.update({"Direction": direction, "Sweep Time": sweep_time, "Sweep Level": round(level, 2)})
        after = d.iloc[si + 1:].reset_index(drop=True)
        if after.empty:
            out.update({"Stage": "SWEEP WATCH", "Reason": "Sweep detected; waiting for reversal candle"})
            return out

        # Reversal confirmation within three bars of sweep.
        rev_i = None
        for j in range(min(3, len(after))):
            r = after.iloc[j]
            if direction == "BUY" and float(r["Close"]) > float(r["Open"]) and float(r["Close"]) > level:
                rev_i = si + 1 + j; break
            if direction == "SELL" and float(r["Close"]) < float(r["Open"]) and float(r["Close"]) < level:
                rev_i = si + 1 + j; break
        if rev_i is None:
            out.update({"Stage": "SWEEP WATCH", "Reason": "Sweep found; reversal candle not confirmed"})
            return out
        out["Stage"] = "REVERSAL CONFIRMED"

        # Breakout reference is a post-reversal structure level; exclude current bar.
        breakout_search_end = n - 1
        post_rev = d.iloc[rev_i + 1:breakout_search_end]
        if len(post_rev) < 2:
            out["Reason"] = "Reversal confirmed; waiting for structure breakout"
            out["Setup Score"] = 45
            return out
        breakout_level = float(post_rev["High"].iloc[:-1].max()) if direction == "BUY" else float(post_rev["Low"].iloc[:-1].min())
        # Identify a breakout candle after reversal and before the latest pullback candle.
        breakout_i = None
        for k in range(rev_i + 1, n - 1):
            prev_window = d.iloc[max(rev_i + 1, k - 5):k]
            if prev_window.empty:
                continue
            if direction == "BUY" and float(d.iloc[k]["Close"]) > float(prev_window["High"].max()):
                breakout_i = k; breakout_level = float(prev_window["High"].max())
            elif direction == "SELL" and float(d.iloc[k]["Close"]) < float(prev_window["Low"].min()):
                breakout_i = k; breakout_level = float(prev_window["Low"].min())
        if breakout_i is None:
            out["Reason"] = "Reversal confirmed; waiting for breakout close"
            out["Setup Score"] = 50
            return out
        out["Stage"] = "BREAKOUT CONFIRMED"
        out["Breakout Level"] = round(breakout_level, 2)

        last = d.iloc[-1]
        close = float(last["Close"])
        tolerance = max(atr * 0.30, abs(breakout_level) * 0.001)
        if direction == "BUY":
            retest = float(last["Low"]) <= breakout_level + tolerance and close >= breakout_level and close > float(last["Open"])
            trend_ok = trend in ("UP", "UNKNOWN")
            stop = min(extreme, float(last["Low"])) - atr * 0.15
            entry = close
            target = entry + (entry - stop) * 1.5
        else:
            retest = float(last["High"]) >= breakout_level - tolerance and close <= breakout_level and close < float(last["Open"])
            trend_ok = trend in ("DOWN", "UNKNOWN")
            stop = max(extreme, float(last["High"])) + atr * 0.15
            entry = close
            target = entry - (stop - entry) * 1.5
        # Require breakout to precede the latest bar and a sensible retest; avoid chasing.
        if breakout_i >= n - 1:
            retest = False
        score = 55 + (15 if rvol >= 1.3 else 0) + (15 if trend_ok and trend != "UNKNOWN" else 0) + (15 if retest else 0)
        out["Setup Score"] = min(score, 100)
        if retest and trend_ok and rvol >= 1.0 and stop > 0 and ((entry > stop) if direction == "BUY" else (entry < stop)):
            out.update({"Stage": "PULLBACK CONFIRMED", "Final Signal": f"CONFIRMED {direction}",
                        "Entry": round(entry, 2), "Stop Loss": round(stop, 2),
                        "Target 1.5R": round(target, 2),
                        "Reason": "Sweep + reversal + breakout + retest + volume/trend filters"})
        else:
            out["Reason"] = "Breakout found; waiting for valid pullback/trend/volume confirmation"
        return out
    except Exception as e:
        out["Reason"] = f"Confirmation engine error: {type(e).__name__}: {e}"
        return out


def detect_live_sudden_move(df: pd.DataFrame) -> Dict[str, Any]:
    """Detect CURRENT sudden 5M BUY/SELL movement. No consolidation required."""
    result = {
        "signal": "NO MOVE", "direction": "NONE", "score": 0.0,
        "reason": "", "move_pct": 0.0, "body_pct": 0.0,
        "body_atr": 0.0, "rvol": 0.0, "structure": "NONE",
        "hh_hl": False, "lh_ll": False, "price_acceleration": 0.0,
        "volume_spike": False,
    }
    if df is None or len(df) < 12:
        result["reason"] = "Insufficient recent 5M candles"
        return result
    try:
        d = df.reset_index(drop=True).copy()
        last = d.iloc[-1]
        o, h, l, c, v = map(float, [last["Open"], last["High"], last["Low"], last["Close"], last["Volume"]])
        prev = float(d["Close"].iloc[-2])
        if min(o, c, prev) <= 0:
            result["reason"] = "Invalid price data"
            return result

        move_pct = ((c - prev) / prev) * 100.0
        candle_range = max(h - l, 1e-9)
        body = abs(c - o)
        body_pct = body / candle_range * 100.0
        atr_s = calculate_atr(d, 14)
        atr = float(atr_s.iloc[-1]) if len(atr_s) and pd.notna(atr_s.iloc[-1]) else max(c * 0.003, 0.01)
        body_atr = body / atr if atr > 0 else 0.0

        vol_base = float(d["Volume"].iloc[-11:-1].mean()) if len(d) >= 11 else float(d["Volume"].iloc[:-1].mean())
        rvol = v / vol_base if vol_base > 0 else 0.0

        # Recent acceleration: current candle move versus average of last few candle moves.
        moves = []
        for i in range(max(1, len(d)-6), len(d)-1):
            pc = float(d["Close"].iloc[i-1]); cc = float(d["Close"].iloc[i])
            if pc > 0:
                moves.append(abs((cc-pc)/pc)*100.0)
        avg_move = float(np.mean(moves)) if moves else 0.0
        acceleration = abs(move_pct) / avg_move if avg_move > 0 else 0.0

        # Recent market structure, not old daily/history setup.
        ph, pl = _confirmed_pivots(d.tail(20), left=1, right=1)
        hh_hl = lh_ll = False
        if len(ph) >= 2 and len(pl) >= 2:
            hh_hl = ph[-1][1] > ph[-2][1] and pl[-1][1] > pl[-2][1]
            lh_ll = ph[-1][1] < ph[-2][1] and pl[-1][1] < pl[-2][1]
        structure = "HH/HL" if hh_hl else "LH/LL" if lh_ll else "NONE"

        bullish = c > o
        bearish = c < o
        volume_spike = rvol >= LIVE_MOVE_MIN_RVOL
        strong_volume = rvol >= LIVE_MOVE_STRONG_RVOL
        strong_body = body_pct >= LIVE_MOVE_MIN_BODY_PCT
        very_strong_body = body_pct >= 65.0
        acceleration_ok = acceleration >= 1.30

        buy = 0.0
        sell = 0.0
        if bullish: buy += 20
        if move_pct >= LIVE_MOVE_MIN_PCT: buy += 20
        if move_pct >= LIVE_MOVE_BIG_PCT: buy += 10
        if bullish and strong_body: buy += 10
        if bullish and very_strong_body: buy += 5
        if volume_spike: buy += 10
        if strong_volume: buy += 5
        if move_pct > 0 and acceleration_ok: buy += 10
        if hh_hl: buy += 10

        if bearish: sell += 20
        if move_pct <= -LIVE_MOVE_MIN_PCT: sell += 20
        if move_pct <= -LIVE_MOVE_BIG_PCT: sell += 10
        if bearish and strong_body: sell += 10
        if bearish and very_strong_body: sell += 5
        if volume_spike: sell += 10
        if strong_volume: sell += 5
        if move_pct < 0 and acceleration_ok: sell += 10
        if lh_ll: sell += 10

        buy = min(100.0, buy); sell = min(100.0, sell)
        score = max(buy, sell)

        if buy >= LIVE_MOVE_MIN_SCORE and buy > sell:
            direction = "BUY"
            signal = "🔥 BIG BUY" if buy >= LIVE_MOVE_STRONG_SCORE else "🟢 BUY"
            reasons = [f"Move +{move_pct:.2f}%", f"RVOL {rvol:.2f}x", f"Body {body_pct:.0f}%"]
            if hh_hl: reasons.append("HH/HL")
            if acceleration_ok: reasons.append("Acceleration")
            reason = " + ".join(reasons)
            score = buy
        elif sell >= LIVE_MOVE_MIN_SCORE and sell > buy:
            direction = "SELL"
            signal = "🔥 BIG SELL" if sell >= LIVE_MOVE_STRONG_SCORE else "🔴 SELL"
            reasons = [f"Move {move_pct:.2f}%", f"RVOL {rvol:.2f}x", f"Body {body_pct:.0f}%"]
            if lh_ll: reasons.append("LH/LL")
            if acceleration_ok: reasons.append("Acceleration")
            reason = " + ".join(reasons)
            score = sell
        else:
            direction = "NONE"
            signal = "NO MOVE"
            reason = f"Move {move_pct:.2f}% | RVOL {rvol:.2f}x | Body {body_pct:.0f}% | Structure {structure}"

        result.update({
            "signal": signal, "direction": direction, "score": round(score, 1),
            "reason": reason, "move_pct": round(move_pct, 3),
            "body_pct": round(body_pct, 1), "body_atr": round(body_atr, 2),
            "rvol": round(rvol, 2), "structure": structure,
            "hh_hl": hh_hl, "lh_ll": lh_ll,
            "price_acceleration": round(acceleration, 2),
            "volume_spike": volume_spike,
        })
        return result
    except Exception as e:
        result["reason"] = f"ERROR: {str(e)[:120]}"
        return result


def _assess_breakout_quality(df: pd.DataFrame) -> Dict[str, Any]:
    """Rule-based breakout quality / failed-breakout warning using completed OHLCV bars.

    This is a filter, not a guarantee. The reference range excludes the latest candle
    so the current candle cannot manufacture its own breakout level.
    """
    out = {
        "BREAKOUT CHECK": "WAIT", "FALSE BREAKOUT RISK": "UNKNOWN",
        "RALLY OUTLOOK": "WAIT", "EST. CONTINUATION": "N/A",
        "RANGE HIGH": None, "RANGE LOW": None, "BREAKOUT QUALITY SCORE": 0,
        "BREAKOUT CHECK REASON": "Insufficient candles",
    }
    try:
        if df is None or len(df) < 25:
            return out
        d = df.reset_index(drop=True).copy()
        required = ["Open", "High", "Low", "Close", "Volume"]
        if any(c not in d.columns for c in required):
            return out
        for col in required:
            d[col] = pd.to_numeric(d[col], errors="coerce")
        d = d.dropna(subset=required).reset_index(drop=True)
        if len(d) < 25:
            return out

        # Use the ten bars immediately before the latest completed bar as the range.
        prior = d.iloc[-11:-1]
        last = d.iloc[-1]
        prev = d.iloc[-2]
        rh, rl = float(prior["High"].max()), float(prior["Low"].min())
        close, opn = float(last["Close"]), float(last["Open"])
        high, low = float(last["High"]), float(last["Low"])
        rng = max(high - low, 1e-9)
        body_pct = abs(close - opn) / rng * 100.0
        close_location = (close - low) / rng  # 0=low of candle, 1=high
        avg_vol = float(d["Volume"].iloc[-21:-1].mean())
        rvol = float(last["Volume"]) / avg_vol if avg_vol > 0 else 0.0
        atr_s = calculate_atr(d, 14)
        atr = float(atr_s.iloc[-1]) if len(atr_s) and pd.notna(atr_s.iloc[-1]) else max(close * 0.001, 0.01)
        prior_close = float(prev["Close"])

        up_break = close > rh
        down_break = close < rl
        # A recent poke outside the range that closes back inside is a failed-breakout warning.
        recent = d.iloc[-4:]
        prior_high = float(d["High"].iloc[-11:-1].max())
        prior_low = float(d["Low"].iloc[-11:-1].min())
        failed_up = (float(recent["High"].max()) > prior_high and close <= rh)
        failed_down = (float(recent["Low"].min()) < prior_low and close >= rl)

        score = 0
        reasons = []
        if up_break:
            direction = "UP"
            if close > rh + 0.05 * atr: score += 20; reasons.append("close beyond range high")
            if body_pct >= 55 and close_location >= 0.70: score += 20; reasons.append("strong bullish close")
            if rvol >= 1.5: score += 25; reasons.append(f"volume confirmation {rvol:.2f}x")
            if close - rh <= 1.5 * atr: score += 10; reasons.append("not excessively extended")
            if failed_up: score -= 35; reasons.append("recent upside rejection")
        elif down_break:
            direction = "DOWN"
            if close < rl - 0.05 * atr: score += 20; reasons.append("close beyond range low")
            if body_pct >= 55 and close_location <= 0.30: score += 20; reasons.append("strong bearish close")
            if rvol >= 1.5: score += 25; reasons.append(f"volume confirmation {rvol:.2f}x")
            if rl - close <= 1.5 * atr: score += 10; reasons.append("not excessively extended")
            if failed_down: score -= 35; reasons.append("recent downside rejection")
        else:
            direction = "NONE"
            if failed_up or failed_down:
                out["BREAKOUT CHECK"] = "FAILED BREAKOUT"
                out["FALSE BREAKOUT RISK"] = "HIGH"
                reasons.append("price returned inside prior range after a wick-through")
            else:
                out["BREAKOUT CHECK"] = "WAIT — RANGE"
                out["FALSE BREAKOUT RISK"] = "MEDIUM"
                reasons.append("no confirmed close outside prior range")

        if direction in ("UP", "DOWN"):
            risk = "LOW" if score >= 55 else "MEDIUM" if score >= 30 else "HIGH"
            out["BREAKOUT CHECK"] = f"{direction} BREAKOUT — CONFIRM" if score >= 45 else f"{direction} BREAKOUT — WEAK"
            out["FALSE BREAKOUT RISK"] = risk
            if score >= 55:
                out["RALLY OUTLOOK"] = "CONTINUATION SETUP" if direction == "UP" else "DOWNSIDE CONTINUATION SETUP"
                out["EST. CONTINUATION"] = "15–30 min watch" if rvol >= 1.5 else "5–15 min watch"
            elif score >= 30:
                out["RALLY OUTLOOK"] = "WAIT FOR RETEST"
                out["EST. CONTINUATION"] = "Not reliable yet"
            else:
                out["RALLY OUTLOOK"] = "AVOID CHASING"
                out["EST. CONTINUATION"] = "Not reliable"
        else:
            out["RALLY OUTLOOK"] = "WAIT FOR CONFIRMATION"
            out["EST. CONTINUATION"] = "N/A"

        out.update({
            "RANGE HIGH": round(rh, 2), "RANGE LOW": round(rl, 2),
            "BREAKOUT QUALITY SCORE": max(0, min(100, int(score))),
            "BREAKOUT CHECK REASON": " | ".join(reasons),
        })
        return out
    except Exception as exc:
        out["BREAKOUT CHECK REASON"] = f"Assessment error: {type(exc).__name__}"
        return out




def _detect_pre_move_volume_setup(df: pd.DataFrame) -> Dict[str, Any]:
    """Find unusual volume building while price is still near a short-term range.

    This is a heuristic early-warning signal, not a prediction or proof of future movement.
    Uses completed OHLCV bars; excludes the latest candle from the reference range.
    """
    out = {
        "PRE-MOVE VOLUME STATUS": "⚪ WAIT",
        "PRE-MOVE VOLUME SCORE": 0,
        "VOLUME BUILDUP X": 0.0,
        "RANGE COMPRESSION %": 0.0,
        "DISTANCE TO RANGE HIGH %": None,
        "DISTANCE TO RANGE LOW %": None,
        "PRE-MOVE VOLUME DIRECTION": "NONE",
        "PRE-MOVE VOLUME REASON": "Need at least 25 completed 5M candles",
    }
    try:
        if df is None or len(df) < 25:
            return out
        d = df.reset_index(drop=True).copy()
        required = ["Open", "High", "Low", "Close", "Volume"]
        if any(c not in d.columns for c in required):
            out["PRE-MOVE VOLUME REASON"] = "Missing OHLCV columns"
            return out
        d[required] = d[required].apply(pd.to_numeric, errors="coerce")
        d = d.dropna(subset=required).reset_index(drop=True)
        if len(d) < 25:
            return out

        # Reference range uses the previous 10 bars; latest candle excluded.
        prior = d.iloc[-11:-1]
        last = d.iloc[-1]
        base_vol = float(d["Volume"].iloc[-21:-4].mean())
        recent_vol = float(d["Volume"].iloc[-4:-1].mean())
        if base_vol <= 0:
            out["PRE-MOVE VOLUME REASON"] = "Volume baseline unavailable"
            return out
        buildup = recent_vol / base_vol
        rh = float(prior["High"].max())
        rl = float(prior["Low"].min())
        close = float(last["Close"])
        mid = (rh + rl) / 2.0
        range_pct = ((rh - rl) / mid * 100.0) if mid > 0 else 0.0
        dist_high = ((rh - close) / rh * 100.0) if rh > 0 else None
        dist_low = ((close - rl) / rl * 100.0) if rl > 0 else None

        score = 0
        reasons = []
        if buildup >= 2.0:
            score += 35; reasons.append(f"volume buildup {buildup:.2f}x")
        elif buildup >= 1.5:
            score += 28; reasons.append(f"volume buildup {buildup:.2f}x")
        elif buildup >= 1.2:
            score += 18; reasons.append(f"volume buildup {buildup:.2f}x")
        else:
            reasons.append(f"volume buildup only {buildup:.2f}x")

        if range_pct <= 1.0:
            score += 25; reasons.append(f"tight range {range_pct:.2f}%")
        elif range_pct <= 1.8:
            score += 18; reasons.append(f"compressed range {range_pct:.2f}%")
        elif range_pct <= 2.5:
            score += 8; reasons.append(f"range {range_pct:.2f}%")

        # Close-to-edge while not yet broken: useful for early warning, not confirmation.
        near_high = dist_high is not None and -0.05 <= dist_high <= 0.50
        near_low = dist_low is not None and -0.05 <= dist_low <= 0.50
        last3_change = float(d["Close"].iloc[-1] / d["Close"].iloc[-4] - 1.0) * 100.0
        direction = "NONE"
        if near_high and close <= rh and last3_change > 0:
            direction = "BUY"
            score += 25; reasons.append("price near range high; breakout not confirmed")
        elif near_low and close >= rl and last3_change < 0:
            direction = "SELL"
            score += 25; reasons.append("price near range low; breakdown not confirmed")
        elif last3_change >= 0.15:
            direction = "BUY"
            score += 10; reasons.append("short-term price pressure up")
        elif last3_change <= -0.15:
            direction = "SELL"
            score += 10; reasons.append("short-term price pressure down")
        else:
            reasons.append("direction not clear")

        # Only call it a pre-move watch when volume is rising and range is reasonably tight.
        if buildup >= 1.5 and range_pct <= 2.5 and score >= 50 and direction == "BUY":
            status = "🟢 BIG VOLUME BUY WATCH"
        elif buildup >= 1.5 and range_pct <= 2.5 and score >= 50 and direction == "SELL":
            status = "🔴 BIG VOLUME SELL WATCH"
        elif buildup >= 1.2 and range_pct <= 3.0 and score >= 35:
            status = "🟡 VOLUME BUILDUP — WAIT"
        else:
            status = "⚪ WAIT"

        out.update({
            "PRE-MOVE VOLUME STATUS": status,
            "PRE-MOVE VOLUME SCORE": int(min(100, score)),
            "VOLUME BUILDUP X": round(buildup, 2),
            "RANGE COMPRESSION %": round(range_pct, 2),
            "DISTANCE TO RANGE HIGH %": round(dist_high, 2) if dist_high is not None else None,
            "DISTANCE TO RANGE LOW %": round(dist_low, 2) if dist_low is not None else None,
            "PRE-MOVE VOLUME DIRECTION": direction,
            "PRE-MOVE VOLUME REASON": " | ".join(reasons),
        })
        return out
    except Exception as e:
        out["PRE-MOVE VOLUME REASON"] = f"Volume setup error: {type(e).__name__}"
        return out



# ================================================================
# EARLY BIG MOVE WATCH — independent UI summary, no old tab changes
# ================================================================
def _early_move_table(momentum_df: pd.DataFrame) -> pd.DataFrame:
    """Rank unbroken consolidation candidates; scores are NOT probabilities."""
    columns = ["STOCK NAME", "LTP", "ENERGY SCORE", "VOLUME BUILDUP X",
               "5M DIRECTION", "15M DIRECTION", "PRE-MOVE STATUS", "RANGE COMPRESSION %",
               "DISTANCE TO RANGE HIGH %", "DISTANCE TO RANGE LOW %", "SIGNAL TIME"]
    if momentum_df is None or momentum_df.empty:
        return pd.DataFrame(columns=columns)
    src = momentum_df.copy()
    def nums(col, default=0):
        return pd.to_numeric(src[col], errors="coerce").fillna(default) if col in src else pd.Series(default, index=src.index, dtype=float)
    symbol_col = next((c for c in ["STOCK NAME", "Symbol", "SYMBOL", "symbol"] if c in src), None)
    if symbol_col is None:
        return pd.DataFrame(columns=columns)
    vol = nums("VOLUME BUILDUP X")
    comp = nums("RANGE COMPRESSION %", 100)
    high_dist = nums("DISTANCE TO RANGE HIGH %", 100)
    low_dist = nums("DISTANCE TO RANGE LOW %", 100)
    base_score = nums("PRE-MOVE VOLUME SCORE")
    raw_dir = src["PRE-MOVE VOLUME DIRECTION"].astype(str).str.upper() if "PRE-MOVE VOLUME DIRECTION" in src else pd.Series("NONE", index=src.index)
    before = src["BEFORE MOVE DIRECTION"].astype(str).str.upper() if "BEFORE MOVE DIRECTION" in src else pd.Series("NONE", index=src.index)
    direction = raw_dir.where(raw_dir.isin(["BUY", "SELL"]), before)
    # The reference range must not have been broken already.
    in_range = (high_dist >= 0) & (low_dist >= 0)
    near_edge = ((direction.eq("BUY") & high_dist.le(0.5)) |
                 (direction.eq("SELL") & low_dist.le(0.5)))
    # ENERGY is a heuristic, distinct from prediction confidence.
    energy = (base_score.clip(0, 80) + (near_edge & in_range).astype(int)*15 +
              (vol.ge(1.5) & comp.le(2.5)).astype(int)*5).clip(0, 100)
    status = np.select([
        in_range & near_edge & vol.ge(1.5) & comp.le(2.5) & energy.ge(70),
        in_range & vol.ge(1.2) & comp.le(3.0) & energy.ge(35),
    ], ["🟠 PRE-MOVE WATCH", "🟡 ENERGY BUILDING"], default="⚪ WAIT")
    out = pd.DataFrame({
        "STOCK NAME": src[symbol_col].astype(str),
        "LTP": nums("LTP"),
        "ENERGY SCORE": energy.round().astype(int),
        "VOLUME BUILDUP X": vol.round(2),
        "5M DIRECTION": direction.where(direction.isin(["BUY", "SELL"]), "NONE"),
        "15M DIRECTION": "NOT CHECKED",
        "PRE-MOVE STATUS": status,
        "RANGE COMPRESSION %": comp.round(2),
        "DISTANCE TO RANGE HIGH %": high_dist.round(2),
        "DISTANCE TO RANGE LOW %": low_dist.round(2),
        "SIGNAL TIME": src["SIGNAL TIME"].astype(str) if "SIGNAL TIME" in src else "N/A",
    })
    return out.sort_values(["ENERGY SCORE", "VOLUME BUILDUP X"], ascending=False).reset_index(drop=True)


def _early_15m_direction(fyers, symbol: str) -> str:
    """Trend estimate from completed 15M candles; no guaranteed next move."""
    try:
        d = _fetch_timeframe_data(fyers, symbol, "15", lookback_days=7)
        if d is None or len(d) < 25:
            return "INSUFFICIENT DATA"
        close = pd.to_numeric(d["Close"], errors="coerce").dropna()
        if len(close) < 25:
            return "INSUFFICIENT DATA"
        ema9 = close.ewm(span=9, adjust=False).mean().iloc[-1]
        ema21 = close.ewm(span=21, adjust=False).mean().iloc[-1]
        last = close.iloc[-1]
        if last > ema9 > ema21:
            return "BUY"
        if last < ema9 < ema21:
            return "SELL"
        return "NEUTRAL"
    except Exception:
        return "DATA ERROR"


def _show_early_big_move_tab(fyers, all_symbols, fo_symbols):
    st.markdown("### ⚡ EARLY BIG MOVE WATCH — BEFORE BREAKOUT")
    st.caption("Energy buildup and price proximity are screening heuristics, not win rates or future-price guarantees.")
    c1, c2, c3 = st.columns([2,2,1])
    with c1:
        selection = st.selectbox("Select universe", ["NSE Stocks", "F&O Stocks"], key="early_universe")
    universe = all_symbols if selection == "NSE Stocks" else fo_symbols
    if not universe:
        st.warning("No symbols available for this universe")
        return
    with c2:
        limit = st.number_input("Stocks to scan", min_value=1, max_value=len(universe), value=min(100,len(universe)), step=25, key="early_scan_limit")
    with c3:
        st.metric("Available", len(universe))
    if st.button("⚡ RUN EARLY BIG MOVE SCAN", key="early_big_move_run", type="primary", use_container_width=True):
        with st.spinner("Scanning completed 5M candles; then checking 15M trend for top candidates..."):
            results, errors, stats = run_momentum_scan(fyers, universe[:limit], is_fo=(selection=="F&O Stocks"))
            base = _early_move_table(pd.DataFrame(results))
            # Avoid numerous extra requests: limit 15M verification to 30 highest-ranked watch candidates.
            candidates = base.index[base["PRE-MOVE STATUS"].ne("⚪ WAIT")][:30]
            for i in candidates:
                symbol = str(base.at[i, "STOCK NAME"])
                try:
                    base.at[i, "15M DIRECTION"] = _early_15m_direction(fyers, symbol)
                except Exception:
                    base.at[i, "15M DIRECTION"] = "DATA ERROR"
            st.session_state["early_move_df"] = base
            st.session_state["early_move_stats"] = stats
            st.session_state["early_move_time"] = _generated_timestamp()
            st.session_state["early_move_errors"] = errors
    table = st.session_state.get("early_move_df")
    if table is None:
        st.info("Select NSE/F&O, then press RUN EARLY BIG MOVE SCAN. Existing tabs remain unchanged.")
        return
    st.caption(f"Last scan: {st.session_state.get('early_move_time','N/A')} | 15M verification: top 30 watch candidates only")
    if table.empty:
        st.warning("No valid stock data returned. Check FYERS token, session, and error logs.")
        return
    f1, f2 = st.columns(2)
    with f1:
        status_filter = st.selectbox("Show candidates", ["PRE-MOVE WATCH + ENERGY BUILDING", "PRE-MOVE WATCH", "ENERGY BUILDING", "ALL"], key="early_status_filter")
    with f2:
        mtf_filter = st.checkbox("Only 5M / 15M matching direction", value=False, key="early_mtf_filter")
    view = table.copy()
    if status_filter == "PRE-MOVE WATCH":
        view = view[view["PRE-MOVE STATUS"].str.contains("PRE-MOVE WATCH", na=False)]
    elif status_filter == "ENERGY BUILDING":
        view = view[view["PRE-MOVE STATUS"].str.contains("ENERGY BUILDING", na=False)]
    elif status_filter != "ALL":
        view = view[view["PRE-MOVE STATUS"].ne("⚪ WAIT")]
    if mtf_filter:
        view = view[view["5M DIRECTION"].eq(view["15M DIRECTION"]) & view["5M DIRECTION"].isin(["BUY", "SELL"])]
    st.metric("Candidates shown", len(view))
    st.dataframe(view, use_container_width=True, height=520, hide_index=True)
    st.download_button("📥 DOWNLOAD EARLY MOVE EXCEL", data=_format_excel_output(view, "EARLY_BIG_MOVE"),
        file_name=f"EARLY_BIG_MOVE_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="early_move_download")
    st.caption("15M NOT CHECKED = not validated, not an agreement. Watch signals require separate confirmation and backtesting.")


def _fetch_momentum_signal(fyers, symbol: str, is_fo: bool = False):
    """Intraday movement worker. Checks recent completed 5M candles and adds early-warning status."""
    stock_ticker = symbol.replace("NSE:", "").replace("-EQ", "") if isinstance(symbol,str) else str(symbol)
    if not isinstance(symbol,str) or not _VALID_EQ_SYMBOL_RE.match(symbol): return None, f"{symbol}: invalid format"
    try:
        df5=_fetch_timeframe_data(fyers,symbol,"5",lookback_days=LIVE_MOVE_LOOKBACK_DAYS)
        if df5 is None or len(df5)<25: return None, f"{symbol}: insufficient 5M data"
        d=df5.reset_index(drop=True).copy()
        block=detect_block_order_activity(d)
        pre=_pre_move_signal(d, block=block)
        before=_before_move_signal(d, pre)
        pre_volume=_detect_pre_move_volume_setup(d)
        breakout_check=_assess_breakout_quality(d)
        # Search the latest 12 completed candles so a signal is not lost on the next candle.
        candidates=[]
        for idx in range(max(12,len(d)-12),len(d)):
            sub=d.iloc[:idx+1]
            mv=detect_live_sudden_move(sub)
            if mv.get("direction") in ("BUY","SELL"):
                candidates.append((idx,mv))
        if candidates:
            idx,mv=max(candidates,key=lambda x:x[0])
            # Always use the actual completed candle timestamp. d has a
            # RangeIndex after reset_index(), so d.index[idx] is NOT time.
            sig_time=d["Time"].iloc[idx] if "Time" in d.columns else _now_ist()
            try:
                sig_time=pd.Timestamp(sig_time).to_pydatetime()
                if sig_time.tzinfo is None: sig_time=sig_time.replace(tzinfo=_now_ist().tzinfo)
            except Exception: sig_time=_now_ist()
            age=max(0,( _now_ist()-sig_time).total_seconds()/60)
            signal=mv["signal"]
        else:
            mv=detect_live_sudden_move(d)
            idx=len(d)-1
            # Latest completed candle timestamp, not scanner refresh time.
            sig_time=d["Time"].iloc[-1] if "Time" in d.columns else _now_ist()
            try:
                sig_time=pd.Timestamp(sig_time).to_pydatetime()
                if sig_time.tzinfo is None: sig_time=sig_time.replace(tzinfo=_now_ist().tzinfo)
            except Exception: sig_time=_now_ist()
            age=max(0,(_now_ist()-sig_time).total_seconds()/60)
            signal=pre["status"] if pre["direction"] in ("BUY","SELL") else (pre_volume["PRE-MOVE VOLUME STATUS"] if pre_volume["PRE-MOVE VOLUME STATUS"] != "⚪ WAIT" else "NO MOVE")
        ltp=float(d["Close"].iloc[-1])
        result={"Symbol":stock_ticker,"LTP":round(ltp,2),"SIGNAL TIME":sig_time.strftime("%d-%b-%Y %H:%M:%S"),"SIGNAL AGE (MIN)":round(age,1),"SIGNAL":signal,"DIRECTION":mv.get("direction") if candidates else pre["direction"],"MOVE %":mv.get("move_pct",0.0),"BODY %":mv.get("body_pct",0.0),"BODY / ATR":mv.get("body_atr",0.0),"RVOL":mv.get("rvol",0.0),"STRUCTURE":mv.get("structure", "NONE"),"HH/HL":"✅" if mv.get("hh_hl") else "−","LH/LL":"✅" if mv.get("lh_ll") else "−","ACCELERATION":mv.get("price_acceleration",0.0),"VOLUME SPIKE":"🔥" if mv.get("volume_spike") else "−","SCORE":mv.get("score",0.0),"BEFORE MOVE SIGNAL":before["signal"],"BEFORE MOVE DIRECTION":before["direction"],"BEFORE MOVE SCORE":before["score"],"BEFORE MOVE REASON":before["reason"],"BEFORE MOVE TRIGGER":before["trigger"],"PRE-MOVE":pre["direction"],"PRE-MOVE SCORE":pre["score"],"PRE-MOVE STATUS":pre["status"],"PRE BUY SCORE":pre.get("buy_score",0),"PRE SELL SCORE":pre.get("sell_score",0),"PRE SCORE GAP":pre.get("score_gap",0),"PRE-MOVE REASON":pre["reason"],"BREAKOUT LEVEL":pre.get("breakout_level"),"BREAKDOWN LEVEL":pre.get("breakdown_level"),"PRE-MOVE RVOL":pre.get("rvol",0),"PRE-MOVE VOLUME STATUS":pre_volume["PRE-MOVE VOLUME STATUS"],"PRE-MOVE VOLUME SCORE":pre_volume["PRE-MOVE VOLUME SCORE"],"VOLUME BUILDUP X":pre_volume["VOLUME BUILDUP X"],"RANGE COMPRESSION %":pre_volume["RANGE COMPRESSION %"],"DISTANCE TO RANGE HIGH %":pre_volume["DISTANCE TO RANGE HIGH %"],"DISTANCE TO RANGE LOW %":pre_volume["DISTANCE TO RANGE LOW %"],"PRE-MOVE VOLUME DIRECTION":pre_volume["PRE-MOVE VOLUME DIRECTION"],"PRE-MOVE VOLUME REASON":pre_volume["PRE-MOVE VOLUME REASON"],"BLOCK ORDER SCORE":block["block_score"],"BLOCK ACTIVITY":block["block_signal"],"BLOCK SIDE":block["block_side"],"BLOCK LEVEL":block["block_level"],"BLOCK RVOL":block["block_rvol"],"BLOCK REASON":block["block_reason"],"REASON":mv.get("reason",pre["reason"]),"MOVEMENT STATUS": signal if candidates else (pre["status"] if pre["direction"] in ("BUY","SELL") else pre_volume["PRE-MOVE VOLUME STATUS"]),**breakout_check}
        if is_fo and result["DIRECTION"] in ("BUY","SELL"):
            try:
                od=fetch_options_chain_data(fyers,symbol); result["PCR"]=od.get("pcr","N/A"); result["OPTIONS BIAS"]=od.get("options_bias","N/A")
            except: result["PCR"]="N/A"; result["OPTIONS BIAS"]="N/A"
        # Explicitly label price/options conflicts instead of silently mixing
        # two independent signals.
        price_dir=str(result.get("DIRECTION", "NONE")).upper()
        opt_bias=str(result.get("OPTIONS BIAS", "N/A")).upper()
        if price_dir in ("BUY","SELL") and opt_bias not in ("N/A","NONE",""):
            opt_dir = "BUY" if "BULL" in opt_bias or "CALL" in opt_bias else "SELL" if "BEAR" in opt_bias or "PUT" in opt_bias else "NONE"
            if opt_dir == price_dir:
                result["COMBINED BIAS"] = f"{price_dir} + OPTIONS CONFIRM"
            elif opt_dir in ("BUY","SELL"):
                result["COMBINED BIAS"] = f"PRICE {price_dir} / OPTIONS {opt_dir} CONFLICT"
            else:
                result["COMBINED BIAS"] = f"PRICE {price_dir} / OPTIONS {opt_bias}"
        else:
            result["COMBINED BIAS"] = price_dir if price_dir in ("BUY","SELL") else "WAIT"
        # Classify opposite-structure trades as reversals for clarity.
        structure=str(result.get("STRUCTURE", "NONE")).upper()
        if price_dir == "BUY" and structure == "LH/LL":
            result["SIGNAL TYPE"] = "BUY REVERSAL"
        elif price_dir == "SELL" and structure == "HH/HL":
            result["SIGNAL TYPE"] = "SELL REVERSAL"
        else:
            result["SIGNAL TYPE"] = price_dir if price_dir in ("BUY","SELL") else "WAIT"
        return result,None
    except Exception as e:
        logger.exception("LIVE MOMENTUM worker failed for %s",symbol); return None,f"{symbol}: error ({type(e).__name__}: {str(e)[:120]})"

# ════════════════════════════════════════════════════════════════════════════════
# THREADED SCAN FUNCTIONS
# ════════════════════════════════════════════════════════════════════════════════
def run_nse_scan(fyers, symbols):
    """Threaded scan for NSE stocks."""
    symbols = _validate_symbols(symbols)
    results, errors = [], []
    stats = ScanStats(total=len(symbols))
    progress = st.progress(0.0, text=f"Scanning NSE Stocks 0 / {len(symbols)}")
    done = 0
    
    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i:i + BATCH_SIZE]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(_fetch_nse_signal, fyers, s): s for s in batch}
            for future in as_completed(futures):
                try:
                    res, err = future.result()
                except Exception as e:
                    res, err = None, f"{futures[future]}: worker error"
                
                if res:
                    results.append(res)
                if err:
                    errors.append(err)
                stats.record(has_result=bool(res), has_error=bool(err))
                done += 1
                progress.progress(done / max(len(symbols), 1), text=f"Scanning NSE {done} / {len(symbols)}")
        
        if i + BATCH_SIZE < len(symbols):
            time.sleep(BATCH_PAUSE_SECONDS)
    
    progress.empty()

    gc.collect()
    return results, errors, stats

def run_fo_scan(fyers, symbols):
    """Threaded scan for F&O stocks."""
    symbols = _validate_symbols(symbols)
    results, errors = [], []
    stats = ScanStats(total=len(symbols))
    progress = st.progress(0.0, text=f"Scanning F&O Stocks 0 / {len(symbols)}")
    done = 0
    
    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i:i + BATCH_SIZE]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(_fetch_fo_signal, fyers, s): s for s in batch}
            for future in as_completed(futures):
                try:
                    res, err = future.result()
                except Exception as e:
                    res, err = None, f"{futures[future]}: worker error"
                
                if res:
                    results.append(res)
                if err:
                    errors.append(err)
                stats.record(has_result=bool(res), has_error=bool(err))
                done += 1
                progress.progress(done / max(len(symbols), 1), text=f"Scanning F&O {done} / {len(symbols)}")
        
        if i + BATCH_SIZE < len(symbols):
            time.sleep(BATCH_PAUSE_SECONDS)
    
    progress.empty()
    gc.collect()
    return results, errors, stats

def run_momentum_scan(fyers, symbols, is_fo: bool = False):
    """Threaded LIVE sudden movement scan. Returns BUY/SELL only."""
    symbols = _validate_symbols(symbols)
    results, errors = [], []
    stats = ScanStats(total=len(symbols))
    progress = st.progress(0.0, text=f"Scanning Live Movement 0 / {len(symbols)}")
    done = 0

    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i:i + BATCH_SIZE]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(_fetch_momentum_signal, fyers, s, is_fo): s for s in batch}
            for future in as_completed(futures):
                symbol = futures[future]
                try:
                    res, err = future.result()
                except Exception as e:
                    res, err = None, f"{symbol}: worker error: {str(e)[:100]}"
                if res and (res.get("DIRECTION") in ("BUY", "SELL") or str(res.get("MOVEMENT STATUS","")).startswith("🟡") or str(res.get("MOVEMENT STATUS","")).endswith("WATCH") or str(res.get("PRE-MOVE VOLUME STATUS","")) in ("🟢 BIG VOLUME BUY WATCH", "🔴 BIG VOLUME SELL WATCH", "🟡 VOLUME BUILDUP — WAIT")):
                    results.append(res)
                if err:
                    errors.append(err)
                stats.record(has_result=bool(res), has_error=bool(err))
                done += 1
                progress.progress(done / max(len(symbols), 1), text=f"Live Movement {done} / {len(symbols)}")
        if i + BATCH_SIZE < len(symbols):
            time.sleep(BATCH_PAUSE_SECONDS)

    progress.empty()
    gc.collect()
    results.sort(key=lambda x: (float(x.get("SCORE", 0)), abs(float(x.get("MOVE %", 0))), float(x.get("RVOL", 0))), reverse=True)
    return results, errors, stats


# ════════════════════════════════════════════════════════════════════════════════
# 20-MINUTE RANGE CONSOLIDATION → PRE-MOVE / BREAKOUT SCANNER (ADDITIVE)
# Uses 1-minute candles; existing scanner engines are not changed.
# ════════════════════════════════════════════════════════════════════════════════
def _detect_20m_range_setup(df: pd.DataFrame) -> Dict[str, Any]:
    """Analyze 20 completed 1M candles and the latest completed candle separately.

    The 20-candle range is df[-21:-1]; df[-1] is evaluated for pre-move pressure
    or a confirmed close beyond that range. This is a heuristic, not a guarantee.
    """
    out = {
        "20M STATUS": "⚪ WAIT", "20M DIRECTION": "NONE", "20M PRE-MOVE SCORE": 0,
        "20M RANGE %": None, "20M RANGE HIGH": None, "20M RANGE LOW": None,
        "20M VOLUME BUILDUP X": 0.0, "20M BUY PRESSURE %": 50.0,
        "20M SELL PRESSURE %": 50.0, "20M DIST TO HIGH %": None,
        "20M DIST TO LOW %": None, "20M BREAKOUT STATUS": "NOT CONFIRMED",
        "20M REASON": "Need at least 21 completed 1M candles", "20M CANDLE TIME": ""
    }
    try:
        required = ["Open", "High", "Low", "Close", "Volume"]
        if df is None or len(df) < 21 or any(c not in df.columns for c in required):
            return out
        d = df.reset_index(drop=True).copy()
        d[required] = d[required].apply(pd.to_numeric, errors="coerce")
        d = d.dropna(subset=required).reset_index(drop=True)
        if len(d) < 21:
            return out
        # The latest candle is tested against the preceding 20-candle range.
        base = d.iloc[-21:-1]
        last = d.iloc[-1]
        rh = float(base["High"].max()); rl = float(base["Low"].min())
        close = float(last["Close"]); open_ = float(last["Open"])
        if rh <= 0 or rl <= 0 or close <= 0 or rh <= rl:
            out["20M REASON"] = "Invalid price range"
            return out
        mid = (rh + rl) / 2.0
        range_pct = (rh - rl) / mid * 100.0
        dist_high = (rh - close) / rh * 100.0
        dist_low = (close - rl) / rl * 100.0
        vols = d["Volume"].clip(lower=0)
        base_vol = float(vols.iloc[-21:-4].mean())
        recent_vol = float(vols.iloc[-4:-1].mean())
        buildup = recent_vol / base_vol if base_vol > 0 else 0.0
        # Candle-body-volume pressure proxy (not actual exchange order-book depth).
        sample = d.iloc[-20:]
        up_vol = float(sample.loc[sample["Close"] > sample["Open"], "Volume"].sum())
        down_vol = float(sample.loc[sample["Close"] < sample["Open"], "Volume"].sum())
        total_pressure_vol = up_vol + down_vol
        buy_pressure = 50.0 if total_pressure_vol <= 0 else 100.0 * up_vol / total_pressure_vol
        sell_pressure = 100.0 - buy_pressure
        score = 0; reasons = []
        if range_pct <= 0.35:
            score += 30; reasons.append(f"very tight 20M range {range_pct:.2f}%")
        elif range_pct <= 0.60:
            score += 25; reasons.append(f"tight 20M range {range_pct:.2f}%")
        elif range_pct <= 0.90:
            score += 15; reasons.append(f"compressed range {range_pct:.2f}%")
        else:
            reasons.append(f"range not tight ({range_pct:.2f}%)")
        if buildup >= 2.0:
            score += 25; reasons.append(f"volume buildup {buildup:.2f}x")
        elif buildup >= 1.5:
            score += 20; reasons.append(f"volume buildup {buildup:.2f}x")
        elif buildup >= 1.2:
            score += 12; reasons.append(f"volume buildup {buildup:.2f}x")
        else:
            reasons.append(f"volume buildup {buildup:.2f}x (weak)")
        # Directional pressure is only a proxy; price location and momentum must agree.
        last3_change = (float(d["Close"].iloc[-1]) / max(float(d["Close"].iloc[-4]), 1e-9) - 1.0) * 100.0
        near_high = -0.10 <= dist_high <= 0.35
        near_low = -0.10 <= dist_low <= 0.35
        direction = "NONE"
        if close > rh and close > open_:
            direction = "BUY"; score += 35
            reasons.append("latest completed candle closed above 20M range high")
        elif close < rl and close < open_:
            direction = "SELL"; score += 35
            reasons.append("latest completed candle closed below 20M range low")
        elif near_high and last3_change > 0 and buy_pressure >= 55:
            direction = "BUY"; score += 20
            reasons.append("near range high with upward price/volume pressure")
        elif near_low and last3_change < 0 and sell_pressure >= 55:
            direction = "SELL"; score += 20
            reasons.append("near range low with downward price/volume pressure")
        elif last3_change >= 0.10 and buy_pressure >= 60:
            direction = "BUY"; score += 10
            reasons.append("short-term buying pressure, but range edge not reached")
        elif last3_change <= -0.10 and sell_pressure >= 60:
            direction = "SELL"; score += 10
            reasons.append("short-term selling pressure, but range edge not reached")
        else:
            reasons.append("direction unclear; wait for range edge or break")
        confirmed = close > rh or close < rl
        if close > rh and direction == "BUY" and buildup >= 1.2 and buy_pressure >= 50:
            status = "🚀 BIG MOVE UP — BREAKOUT CONFIRMED"
            breakout_status = "CONFIRMED UP"
        elif close < rl and direction == "SELL" and buildup >= 1.2 and sell_pressure >= 50:
            status = "🔻 BIG MOVE DOWN — BREAKDOWN CONFIRMED"
            breakout_status = "CONFIRMED DOWN"
        elif range_pct <= 0.90 and buildup >= 1.2 and score >= 45 and direction == "BUY":
            status = "🟢 BIG MOVE UP WATCH"; breakout_status = "WATCH — NOT CONFIRMED"
        elif range_pct <= 0.90 and buildup >= 1.2 and score >= 45 and direction == "SELL":
            status = "🔴 BIG MOVE DOWN WATCH"; breakout_status = "WATCH — NOT CONFIRMED"
        elif range_pct <= 1.2 and buildup >= 1.2:
            status = "🟡 RANGE + VOLUME BUILDUP — WAIT"; breakout_status = "NOT CONFIRMED"
        else:
            status = "⚪ WAIT"; breakout_status = "NOT CONFIRMED"
        candle_time = ""
        if "Time" in d.columns:
            try: candle_time = str(pd.Timestamp(d["Time"].iloc[-1]))
            except Exception: candle_time = str(d["Time"].iloc[-1])
        out.update({
            "20M STATUS": status, "20M DIRECTION": direction,
            "20M PRE-MOVE SCORE": int(min(100, score)), "20M RANGE %": round(range_pct, 3),
            "20M RANGE HIGH": round(rh, 2), "20M RANGE LOW": round(rl, 2),
            "20M VOLUME BUILDUP X": round(buildup, 2),
            "20M BUY PRESSURE %": round(buy_pressure, 1), "20M SELL PRESSURE %": round(sell_pressure, 1),
            "20M DIST TO HIGH %": round(dist_high, 3), "20M DIST TO LOW %": round(dist_low, 3),
            "20M BREAKOUT STATUS": breakout_status,
            "20M REASON": " | ".join(reasons), "20M CANDLE TIME": candle_time
        })
        return out
    except Exception as e:
        out["20M REASON"] = f"20M analysis error: {type(e).__name__}"
        return out


# Small, thread-safe cache for repeated clicks on the independent 20M scan.
# It avoids refetching every symbol immediately after a user reruns the scan.
_RANGE20_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_RANGE20_CACHE_LOCK = threading.Lock()
_RANGE20_CACHE_TTL_SECONDS = 45


def _prepare_20m_completed_session_candles(df1: pd.DataFrame) -> pd.DataFrame:
    """Keep only completed 1-minute candles from the current/latest session.

    During NSE market hours, do not mix yesterday's candles into today's 20-bar
    range. Also drop the still-forming current minute candle to avoid repainting.
    """
    if df1 is None or df1.empty or "Time" not in df1.columns:
        return pd.DataFrame() if df1 is None else df1
    d = df1.copy()
    ts = pd.to_datetime(d["Time"], errors="coerce")
    if getattr(ts.dt, "tz", None) is None:
        ts = ts.dt.tz_localize("UTC").dt.tz_convert(IST)
    else:
        ts = ts.dt.tz_convert(IST)
    d["Time"] = ts
    d = d.dropna(subset=["Time"]).sort_values("Time").reset_index(drop=True)
    if d.empty:
        return d

    now = _now_ist()
    market_open = now.replace(hour=9, minute=15, second=0, microsecond=0)
    market_close = now.replace(hour=15, minute=30, second=0, microsecond=0)
    if now.weekday() < 5 and market_open <= now <= market_close:
        # Intraday: only today's session is valid. If the feed is stale, return
        # insufficient data rather than silently mixing sessions.
        d = d[d["Time"].dt.date == now.date()].copy().reset_index(drop=True)
    else:
        # Outside market hours, use only the most recent session in the feed.
        latest_day = d["Time"].iloc[-1].date()
        d = d[d["Time"].dt.date == latest_day].copy().reset_index(drop=True)

    # Candle timestamps mark candle start. A candle is completed only after its
    # one-minute interval has elapsed; drop a live candle if it is still forming.
    if not d.empty:
        latest_start = d["Time"].iloc[-1]
        if now < latest_start + timedelta(minutes=1):
            d = d.iloc[:-1].copy().reset_index(drop=True)
    return d


def _fetch_20m_range_signal(fyers, symbol: str):
    """Fetch completed same-session 1M candles and detect 20M setup."""
    ticker = symbol.replace("NSE:", "").replace("-EQ", "") if isinstance(symbol, str) else str(symbol)
    if not isinstance(symbol, str) or not _VALID_EQ_SYMBOL_RE.match(symbol):
        return None, f"{symbol}: invalid symbol format"
    try:
        now_ts = time.time()
        with _RANGE20_CACHE_LOCK:
            cached = _RANGE20_CACHE.get(symbol)
            if cached and now_ts - cached[0] <= _RANGE20_CACHE_TTL_SECONDS:
                cached_row = dict(cached[1])
                cached_row["SYMBOL"] = ticker
                return cached_row, None

        # Two-day request is retained for compatibility with the FYERS history
        # endpoint; preprocessing below removes previous-session candles.
        df1 = _fetch_timeframe_data(fyers, symbol, "1", lookback_days=2)
        df1 = _prepare_20m_completed_session_candles(df1)
        if df1 is None or len(df1) < 21:
            return None, f"{symbol}: need 21 completed 1M candles in the latest session"
        setup = _detect_20m_range_setup(df1)
        setup.update({"SYMBOL": ticker, "LTP": round(float(df1["Close"].iloc[-1]), 2)})
        with _RANGE20_CACHE_LOCK:
            _RANGE20_CACHE[symbol] = (time.time(), dict(setup))
        if setup["20M STATUS"] == "⚪ WAIT" and setup["20M PRE-MOVE SCORE"] == 0:
            return setup, None
        return setup, None
    except Exception as e:
        return None, f"{symbol}: {type(e).__name__}: {str(e)[:100]}"


def run_20m_range_scan(fyers, symbols):
    """Scan a symbol universe for 20-minute range setups using 1M candles."""
    symbols = _validate_symbols(list(dict.fromkeys(symbols)))
    results, errors = [], []
    stats = ScanStats(total=len(symbols))
    if not symbols:
        return results, ["No valid symbols in selected universe"], stats
    progress = st.progress(0.0, text=f"20M Range scan 0 / {len(symbols)}")
    done = 0
    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i:i + BATCH_SIZE]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(_fetch_20m_range_signal, fyers, sym): sym for sym in batch}
            for future in as_completed(futures):
                sym = futures[future]
                try:
                    res, err = future.result()
                except Exception as e:
                    res, err = None, f"{sym}: worker error ({type(e).__name__})"
                if res:
                    results.append(res)
                if err:
                    errors.append(err)
                stats.record(has_result=bool(res), has_error=bool(err))
                done += 1
                progress.progress(done / max(len(symbols), 1), text=f"20M Range scan {done} / {len(symbols)}")
        if i + BATCH_SIZE < len(symbols):
            time.sleep(BATCH_PAUSE_SECONDS)
    progress.empty()
    results.sort(key=lambda x: (int(x.get("20M PRE-MOVE SCORE", 0)), float(x.get("20M VOLUME BUILDUP X", 0))), reverse=True)
    return results, errors, stats


# ════════════════════════════════════════════════════════════════════════════════
# PIN RULES — ADDITIONAL LIQUIDITY / REVERSAL / BIG-MOVE ANALYSIS
# Existing scanner logic is intentionally untouched. This tab runs only when used.
# ════════════════════════════════════════════════════════════════════════════════
PIN_MIN_CONFIDENCE = 70
PIN_STRONG_CONFIDENCE = 82
PIN_PIVOT_LEN = 5
PIN_EQUAL_ATR_TOL = 0.15
PIN_BIGMOVE_MIN_SCORE = 70
PIN_MAX_SCAN = None  # Full NSE/F&O universe; no artificial 100-stock limit


def calculate_pin_rules(df_5m: pd.DataFrame, data_5m: Dict[str, Any], data_15m: Dict[str, Any], data_1h: Dict[str, Any]) -> Dict[str, Any]:
    """Rule-based implementation of the supplied Pine 'AI PRO v3' ideas.
    This is NOT machine-learning AI and it does not access the exchange order book.
    """
    out = {
        "PIN SIGNAL": "WAIT", "PIN SCORE": 0.0, "LIQUIDITY": "NONE",
        "BUY LIQUIDITY": "NONE", "SELL LIQUIDITY": "NONE",
        "BUY LIQUIDITY SCORE": 0.0, "SELL LIQUIDITY SCORE": 0.0,
        "TOP LIQUIDITY": "NONE", "TOP LIQUIDITY LEVEL": None,
        "TOP LIQUIDITY SIDE": "BUY-SIDE", "TOP LIQUIDITY STATUS": "NONE",
        "BOTTOM LIQUIDITY": "NONE", "BOTTOM LIQUIDITY LEVEL": None,
        "BOTTOM LIQUIDITY SIDE": "SELL-SIDE", "BOTTOM LIQUIDITY STATUS": "NONE",
        "SWEEP": "NONE", "REVERSAL": "NONE", "EQUAL HIGH": "NO", "EQUAL LOW": "NO",
        "BIG MOVEMENT": "NO", "BIG MOVE SCORE": 0.0, "STRUCTURE": "NONE",
        "AMD PHASE": "INSUFFICIENT_DATA", "AMD DIRECTION": "NONE",
        "AMD SIGNAL": "WAIT", "AMD REASON": "Need at least 20 candles",
        "5M TREND": data_5m.get("structure_trend", "N/A"),
        "15M TREND": data_15m.get("structure_trend", "N/A"),
        "1H TREND": data_1h.get("structure_trend", "N/A"),
        "RVOL": data_5m.get("rvol", 0), "RSI": data_5m.get("rsi", 50),
        "PRESSURE": data_5m.get("pressure_trend", "N/A"), "REASON": ""
    }
    if df_5m is None or len(df_5m) < 30:
        out["REASON"] = "Insufficient 5M candles"
        return out
    try:
        d = df_5m.reset_index(drop=True).copy()
        last = d.iloc[-1]
        o, h, l, c, v = [float(last[x]) for x in ["Open", "High", "Low", "Close", "Volume"]]
        body = abs(c-o)
        rng = max(h-l, 1e-9)
        upper_wick = h-max(o,c)
        lower_wick = min(o,c)-l
        atr_s = calculate_atr(d, 14)
        atr = float(atr_s.iloc[-1]) if pd.notna(atr_s.iloc[-1]) else max(c*0.005, 0.01)
        vwap_s = calculate_vwap(d)
        vwap = float(vwap_s.iloc[-1]) if len(vwap_s) else c
        rsi = float(data_5m.get("rsi", 50) or 50)
        rvol = float(data_5m.get("rvol", 0) or 0)
        macd_bull = bool(data_5m.get("macd_bullish", False))
        ema_trend = data_5m.get("ema_trend", "NEUTRAL")
        structure_trend = data_5m.get("structure_trend", "NEUTRAL")
        bull = c > o; bear = c < o
        strong_bull = bull and body/rng*100 >= 55
        strong_bear = bear and body/rng*100 >= 55

        # Confirmed liquidity pivots. Current candle is never used as a pivot.
        ph, pl = _confirmed_pivots(d, left=PIN_PIVOT_LEN, right=PIN_PIVOT_LEN)
        last_hi = ph[-1][1] if ph else None
        prev_hi = ph[-2][1] if len(ph) >= 2 else None
        last_lo = pl[-1][1] if pl else None
        prev_lo = pl[-2][1] if len(pl) >= 2 else None
        eq_hi = last_hi is not None and prev_hi is not None and abs(last_hi-prev_hi) <= atr*PIN_EQUAL_ATR_TOL
        eq_lo = last_lo is not None and prev_lo is not None and abs(last_lo-prev_lo) <= atr*PIN_EQUAL_ATR_TOL
        sweep_buy = last_hi is not None and h > last_hi and c < last_hi and upper_wick > body
        sweep_sell = last_lo is not None and l < last_lo and c > last_lo and lower_wick > body
        bullish_sweep = sweep_sell
        bearish_sweep = sweep_buy

        bullish_reversal = bullish_sweep and bull and c > vwap and rsi > 45
        bearish_reversal = bearish_sweep and bear and c < vwap and rsi < 55

        # Pine-style confluence score.
        buy = 0.0; sell = 0.0
        buy += 25 if ema_trend == "BULLISH" and structure_trend == "BULLISH" else 15 if structure_trend == "BULLISH" else 0
        sell += 25 if ema_trend == "BEARISH" and structure_trend == "BEARISH" else 15 if structure_trend == "BEARISH" else 0
        buy += 15 if rsi >= 55 else 7 if rsi >= 50 else 0
        sell += 15 if rsi <= 45 else 7 if rsi <= 50 else 0
        buy += 15 if c > vwap else 0; sell += 15 if c < vwap else 0
        buy += 20 if macd_bull and float(data_5m.get("macd_hist", 0) or 0) > 0 else 10 if macd_bull else 0
        sell += 20 if (not macd_bull) and float(data_5m.get("macd_hist", 0) or 0) < 0 else 10 if not macd_bull else 0
        buy += 10 if rvol >= 1.5 and bull else 0
        sell += 10 if rvol >= 1.5 and bear else 0
        buy += 5 if strong_bull else 0; sell += 5 if strong_bear else 0
        buy += 10 if bullish_sweep else 0; sell += 10 if bearish_sweep else 0
        buy += 10 if bullish_reversal else 0; sell += 10 if bearish_reversal else 0
        pin_score = min(100.0, max(buy, sell))
        direction = "BUY" if buy > sell else "SELL" if sell > buy else "WAIT"
        if pin_score >= PIN_STRONG_CONFIDENCE and direction != "WAIT":
            pin_signal = f"{'🟢 STRONG BUY' if direction=='BUY' else '🔴 STRONG SELL'}"
        elif pin_score >= PIN_MIN_CONFIDENCE and direction != "WAIT":
            pin_signal = f"{'🟢 BUY' if direction=='BUY' else '🔴 SELL'}"
        else:
            pin_signal = "🟡 WAIT"

        # Existing BIG MOVE engine is reused; no duplicate scan logic.
        bm = detect_big_move_setup(d)
        amd = _detect_amd_phase(d)
        structure = bm.get("structure", "NONE")
        liquidity = "EQ HIGH" if eq_hi else "EQ LOW" if eq_lo else "HIGH" if last_hi is not None else "LOW" if last_lo is not None else "NONE"

        # Buy-side liquidity normally sits above confirmed swing highs/equal highs;
        # sell-side liquidity normally sits below confirmed swing lows/equal lows.
        # Scores are setup-strength scores, not exchange order-book quantities.
        buy_liq_score = 0.0
        sell_liq_score = 0.0
        if last_hi is not None:
            buy_liq_score += 45
            if eq_hi:
                buy_liq_score += 30
            if h >= last_hi:
                buy_liq_score += 15
            if bearish_sweep:
                buy_liq_score += 10
        if last_lo is not None:
            sell_liq_score += 45
            if eq_lo:
                sell_liq_score += 30
            if l <= last_lo:
                sell_liq_score += 15
            if bullish_sweep:
                sell_liq_score += 10
        buy_liq_score = min(100.0, buy_liq_score)
        sell_liq_score = min(100.0, sell_liq_score)
        buy_liq = ("HIGH" if buy_liq_score >= 70 else "MEDIUM" if buy_liq_score >= 40 else "LOW" if buy_liq_score > 0 else "NONE")
        sell_liq = ("HIGH" if sell_liq_score >= 70 else "MEDIUM" if sell_liq_score >= 40 else "LOW" if sell_liq_score > 0 else "NONE")

        # Explicit TOP/BOTTOM liquidity identification.
        # TOP = buy-side liquidity above the latest confirmed swing/equal high.
        # BOTTOM = sell-side liquidity below the latest confirmed swing/equal low.
        top_liq = "TOP BUY LIQ" if last_hi is not None else "NONE"
        bottom_liq = "BOTTOM SELL LIQ" if last_lo is not None else "NONE"
        top_status = "SWEPT" if bearish_sweep else "ACTIVE" if last_hi is not None else "NONE"
        bottom_status = "SWEPT" if bullish_sweep else "ACTIVE" if last_lo is not None else "NONE"

        sweep = "🟢 LOW SWEPT" if bullish_sweep else "🔴 HIGH SWEPT" if bearish_sweep else "NONE"
        reversal = "🟢 BULL REVERSAL" if bullish_reversal else "🔴 BEAR REVERSAL" if bearish_reversal else "NONE"
        out.update({
            "PIN SIGNAL": pin_signal, "PIN SCORE": round(pin_score, 1),
            "LIQUIDITY": liquidity,
            "BUY LIQUIDITY": buy_liq, "SELL LIQUIDITY": sell_liq,
            "BUY LIQUIDITY SCORE": round(buy_liq_score, 1),
            "SELL LIQUIDITY SCORE": round(sell_liq_score, 1),
            "TOP LIQUIDITY": top_liq,
            "TOP LIQUIDITY LEVEL": round(float(last_hi), 2) if last_hi is not None else None,
            "TOP LIQUIDITY SIDE": "BUY-SIDE",
            "TOP LIQUIDITY STATUS": top_status,
            "BOTTOM LIQUIDITY": bottom_liq,
            "BOTTOM LIQUIDITY LEVEL": round(float(last_lo), 2) if last_lo is not None else None,
            "BOTTOM LIQUIDITY SIDE": "SELL-SIDE",
            "BOTTOM LIQUIDITY STATUS": bottom_status,
            "SWEEP": sweep, "REVERSAL": reversal,
            "EQUAL HIGH": "YES" if eq_hi else "NO", "EQUAL LOW": "YES" if eq_lo else "NO",
            "BIG MOVEMENT": bm.get("signal", "NO BIG MOVE"),
            "BIG MOVE SCORE": bm.get("score", 0.0), "STRUCTURE": structure,
            "AMD PHASE": amd.get("phase", "INSUFFICIENT_DATA"),
            "AMD DIRECTION": amd.get("direction", "NONE"),
            "AMD SIGNAL": amd.get("signal", "WAIT"),
            "AMD REASON": amd.get("reason", "N/A"),
            "REASON": " | ".join([x for x in [
                "EQ HIGH" if eq_hi else "", "EQ LOW" if eq_lo else "",
                "LOW SWEEP" if bullish_sweep else "HIGH SWEEP" if bearish_sweep else "",
                "BULL REVERSAL" if bullish_reversal else "BEAR REVERSAL" if bearish_reversal else "",
                "BIG MOVE" if bm.get("direction") in ["UP", "DOWN"] else ""
            ] if x]) or "No PIN confirmation"
        })
        return out
    except Exception as e:
        out["REASON"] = f"PIN error: {type(e).__name__}"
        return out


def _pin_normalize_symbol(symbol: Any) -> str:
    """Return a valid FYERS NSE equity symbol for PIN analysis."""
    raw = str(symbol or "").strip().upper()
    if not raw:
        return ""
    raw = raw.replace("-EQ", "")
    if raw.startswith("NSE:"):
        raw = raw[4:]
    # Remove accidental whitespace and exchange suffixes from scanner output.
    raw = raw.split("|")[0].strip()
    return f"NSE:{raw}-EQ"


def _pin_display_symbol(symbol: Any) -> str:
    """Return a clean ticker for the PIN results table."""
    raw = str(symbol or "").strip().upper()
    raw = raw.replace("NSE:", "").replace("-EQ", "")
    return raw



# ────────────────────────────────────────────────────────────────────────────────
# FAST TWO-STAGE PIN SCANNER
# Stage 1: ALL stocks -> 5M analysis only
# Stage 2: only promising candidates -> 15M + 1H confirmation
# This keeps PIN independent and avoids ~2 extra requests for weak stocks.
# ────────────────────────────────────────────────────────────────────────────────
PIN_FAST_WORKERS = 8
PIN_FAST_BATCH_SIZE = 80
PIN_CANDIDATE_SCORE = 40.0
PIN_CANDIDATE_MIN_RVOL = 1.05


def _pin_stage1_candidate(fyers, symbol):
    """Cheap first-pass filter using only 5-minute data."""
    display_symbol = _pin_display_symbol(symbol)
    try:
        a5 = analyze_timeframe(fyers, symbol, "5")
        if a5.get("status") != "OK" or a5.get("df") is None or len(a5.get("df")) < 30:
            return None, f"{display_symbol}: 5M unavailable"

        data5 = a5.get("data", {}) or {}
        pin = calculate_pin_rules(a5["df"], data5, {}, {})
        score = float(pin.get("PIN SCORE", 0) or 0)
        sweep = str(pin.get("SWEEP", "NONE")).upper()
        reversal = str(pin.get("REVERSAL", "NONE")).upper()
        big_score = float(pin.get("BIG MOVE SCORE", 0) or 0)
        rvol = float(data5.get("rvol", 0) or 0)

        # Keep candidates with objective evidence; final decision happens in stage 2.
        keep = (
            score >= PIN_CANDIDATE_SCORE
            or sweep != "NONE"
            or reversal != "NONE"
            or big_score >= PIN_BIGMOVE_MIN_SCORE
            or rvol >= PIN_CANDIDATE_MIN_RVOL
        )
        if not keep:
            return None, None

        return {
            "symbol": symbol,
            "display": display_symbol,
            "a5": a5,
            "stage1_score": score,
        }, None
    except Exception as e:
        return None, f"{display_symbol}: {type(e).__name__}: {str(e)[:100]}"


def _pin_stage2_confirm(fyers, candidate):
    """Fetch higher timeframes only for stage-1 candidates."""
    symbol = candidate["symbol"]
    display_symbol = candidate["display"]
    try:
        a15 = analyze_timeframe(fyers, symbol, "15")
        a1h = analyze_timeframe(fyers, symbol, "60")

        a5 = candidate["a5"]
        pin = calculate_pin_rules(
            a5.get("df"),
            a5.get("data", {}) or {},
            a15.get("data", {}) if a15.get("status") == "OK" else {},
            a1h.get("data", {}) if a1h.get("status") == "OK" else {},
        )
        pin["Symbol"] = display_symbol
        pin["LTP"] = (a5.get("data", {}) or {}).get("close", "N/A")
        return pin, None
    except Exception as e:
        return None, f"{display_symbol}: {type(e).__name__}: {str(e)[:100]}"


def _run_pin_scan(fyers, universe, pin_min=PIN_MIN_CONFIDENCE, pin_mode="ALL"):
    """Fast full-universe PIN scan with a 5M prefilter and parallel workers."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    seen = set()
    symbols = []
    for symbol in list(universe or []):
        normalized = _pin_normalize_symbol(symbol)
        if normalized and normalized not in seen:
            seen.add(normalized)
            symbols.append(normalized)

    total = len(symbols)
    if total == 0:
        return pd.DataFrame(), ["No valid symbols available"]

    errors = []
    candidates = []
    progress = st.progress(0.0)
    status = st.empty()

    # STAGE 1: all stocks, one timeframe only
    done = 0
    with ThreadPoolExecutor(max_workers=PIN_FAST_WORKERS) as executor:
        futures = {
            executor.submit(_pin_stage1_candidate, fyers, symbol): symbol
            for symbol in symbols
        }
        for future in as_completed(futures):
            candidate, error = future.result()
            if candidate is not None:
                candidates.append(candidate)
            if error:
                errors.append(error)
            done += 1
            progress.progress(done / total, text=f"Stage 1/2: {done:,}/{total:,} stocks | Candidates: {len(candidates):,}")

    # STAGE 2: candidates only, fetch 15M + 1H
    rows = []
    candidate_total = len(candidates)
    if candidate_total:
        done = 0
        with ThreadPoolExecutor(max_workers=PIN_FAST_WORKERS) as executor:
            futures = {
                executor.submit(_pin_stage2_confirm, fyers, candidate): candidate
                for candidate in candidates
            }
            for future in as_completed(futures):
                row, error = future.result()
                if row is not None:
                    rows.append(row)
                if error:
                    errors.append(error)
                done += 1
                progress.progress(done / candidate_total, text=f"Stage 2/2: {done:,}/{candidate_total:,} candidates | Matches: {len(rows):,}")

    progress.empty()
    status.empty()

    result = pd.DataFrame(rows)
    if result.empty:
        return result, errors

    result["__score"] = pd.to_numeric(result.get("PIN SCORE", 0), errors="coerce").fillna(0)
    result = result[result["__score"] >= float(pin_min)].copy()

    if not result.empty:
        signal_text = result["PIN SIGNAL"].astype(str).str.upper()
        if pin_mode == "BUY ONLY":
            result = result[signal_text.str.contains("BUY", na=False)]
        elif pin_mode == "SELL ONLY":
            result = result[signal_text.str.contains("SELL", na=False)]
        elif pin_mode == "STRONG ONLY":
            result = result[signal_text.str.contains("STRONG", na=False)]

    result = result.drop(columns=["__score"], errors="ignore")
    if not result.empty and "PIN SCORE" in result.columns:
        result = result.sort_values("PIN SCORE", ascending=False).reset_index(drop=True)
    return result, errors

# ════════════════════════════════════════════════════════════════════════════════
# LIVE EXCHANGE ORDER BOOK ENGINE
# ════════════════════════════════════════════════════════════════════════════════
def _safe_float(value, default=0.0):
    try:
        if value is None or value == "":
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _depth_rows(payload, key):
    """Return normalized FYERS depth rows for bids/asks."""
    rows = payload.get(key, []) if isinstance(payload, dict) else []
    if not isinstance(rows, list):
        return []
    out = []
    for row in rows[:DEPTH_TOP_LEVELS]:
        if isinstance(row, dict):
            price = _safe_float(row.get("price", row.get("p", 0)))
            qty = _safe_float(row.get("volume", row.get("qty", row.get("v", 0))))
            orders = _safe_float(row.get("ord", row.get("orders", row.get("o", 0))))
        elif isinstance(row, (list, tuple)) and len(row) >= 2:
            price = _safe_float(row[0])
            qty = _safe_float(row[1])
            orders = _safe_float(row[2]) if len(row) > 2 else 0.0
        else:
            continue
        if price > 0 and qty >= 0:
            out.append({"price": price, "qty": qty, "orders": orders})
    return out


def fetch_live_market_depth(fyers, symbol: str) -> Dict[str, Any]:
    """Fetch one real FYERS exchange market-depth snapshot.

    This is NOT simulated order flow and NOT TradingView footprint data.
    FYERS depth returns bid/ask levels and quantities for the symbol.
    """
    empty = {
        "status": "DATA_UNAVAILABLE", "symbol": symbol,
        "ltp": None, "best_bid": None, "best_ask": None,
        "bid_qty": 0.0, "ask_qty": 0.0, "total_buy_qty": 0.0,
        "total_sell_qty": 0.0, "spread": None, "spread_pct": None,
        "depth_imbalance": 0.0, "buy_score": 0.0, "sell_score": 0.0,
        "direction": "WAIT", "strength": 0.0, "pin": "WAIT",
        "reason": "No live depth data", "bids": [], "asks": []
    }
    try:
        if not isinstance(symbol, str) or not _VALID_EQ_SYMBOL_RE.match(symbol):
            empty["reason"] = "Invalid NSE equity symbol"
            return empty

        resp = fyers.depth({"symbol": symbol, "ohlcv_flag": "1"})
        if not isinstance(resp, dict):
            empty["reason"] = "Invalid FYERS depth response"
            return empty

        api_status = str(resp.get("s", "")).lower()
        api_code = resp.get("code")
        api_message = resp.get("message")
        if api_status == "error" or (api_code not in (None, 0, "0", "")):
            empty["reason"] = f"FYERS depth API {api_code}: {api_message or 'Unknown error'}"
            return empty

        # FYERS v3 normally returns: {"d": {"NSE:SBIN-EQ": {...}}}
        data = resp.get("d", resp)

        if isinstance(data, dict):
            # Select the actual depth object under the exact requested symbol.
            symbol_data = data.get(symbol)
            if isinstance(symbol_data, dict):
                data = symbol_data
            elif len(data) == 1:
                only_value = next(iter(data.values()))
                if isinstance(only_value, dict) and (
                    "bids" in only_value or "ask" in only_value or "asks" in only_value
                ):
                    data = only_value

        if isinstance(data, list):
            data = data[0] if data else {}
        if not isinstance(data, dict):
            data = {}

        if isinstance(data.get("data"), dict):
            data = data["data"]

        bids = _depth_rows(data, "bids")
        asks = _depth_rows(data, "ask")
        if not asks:
            asks = _depth_rows(data, "asks")

        ltp = _safe_float(data.get("ltp", data.get("lp", 0)))
        best_bid = bids[0]["price"] if bids else _safe_float(data.get("bid", 0))
        best_ask = asks[0]["price"] if asks else _safe_float(data.get("ask", 0))

        bid_qty_levels = sum(x["qty"] for x in bids)
        ask_qty_levels = sum(x["qty"] for x in asks)
        total_buy_qty = _safe_float(data.get("totalbuyqty", data.get("total_buy_qty", bid_qty_levels)))
        total_sell_qty = _safe_float(data.get("totalsellqty", data.get("total_sell_qty", ask_qty_levels)))

        # Prefer the exchange totals when present; otherwise use the five visible levels.
        buy_base = total_buy_qty if total_buy_qty > 0 else bid_qty_levels
        sell_base = total_sell_qty if total_sell_qty > 0 else ask_qty_levels
        total = buy_base + sell_base
        imbalance = ((buy_base - sell_base) / total * 100.0) if total > 0 else 0.0

        spread = (best_ask - best_bid) if best_bid > 0 and best_ask > 0 else None
        spread_pct = (spread / ltp * 100.0) if spread is not None and ltp > 0 else None

        # Depth-only direction. It intentionally does not use old candles.
        buy_score = 50.0 + max(0.0, imbalance) * 0.9
        sell_score = 50.0 + max(0.0, -imbalance) * 0.9
        buy_score = min(100.0, buy_score)
        sell_score = min(100.0, sell_score)

        if imbalance >= DEPTH_STRONG_IMBALANCE:
            direction = "BUY"
            strength = min(100.0, 50.0 + imbalance)
        elif imbalance <= -DEPTH_STRONG_IMBALANCE:
            direction = "SELL"
            strength = min(100.0, 50.0 + abs(imbalance))
        elif imbalance >= DEPTH_MIN_IMBALANCE:
            direction = "BUY"
            strength = min(100.0, 50.0 + imbalance * 0.8)
        elif imbalance <= -DEPTH_MIN_IMBALANCE:
            direction = "SELL"
            strength = min(100.0, 50.0 + abs(imbalance) * 0.8)
        else:
            direction = "WAIT"
            strength = 50.0

        pin = "WAIT"
        if direction == "BUY" and imbalance >= DEPTH_STRONG_IMBALANCE:
            pin = "🟢 BUY PIN"
        elif direction == "SELL" and imbalance <= -DEPTH_STRONG_IMBALANCE:
            pin = "🔴 SELL PIN"

        reasons = []
        if buy_base > sell_base:
            reasons.append(f"Bid depth +{imbalance:.1f}%")
        elif sell_base > buy_base:
            reasons.append(f"Ask depth {imbalance:.1f}%")
        if best_bid and best_ask:
            reasons.append(f"Spread {spread:.2f}" if spread is not None else "Best bid/ask")
        if not bids or not asks:
            reasons.append("partial depth")

        return {
            "status": "OK" if bids or asks or ltp > 0 else "DATA_UNAVAILABLE",
            "symbol": symbol,
            "ltp": ltp if ltp > 0 else None,
            "best_bid": best_bid if best_bid > 0 else None,
            "best_ask": best_ask if best_ask > 0 else None,
            "bid_qty": bid_qty_levels,
            "ask_qty": ask_qty_levels,
            "total_buy_qty": buy_base,
            "total_sell_qty": sell_base,
            "spread": spread,
            "spread_pct": spread_pct,
            "depth_imbalance": round(imbalance, 2),
            "buy_score": round(buy_score, 1),
            "sell_score": round(sell_score, 1),
            "direction": direction,
            "strength": round(strength, 1),
            "pin": pin,
            "reason": " | ".join(reasons) if reasons else "Live depth snapshot",
            "bids": bids,
            "asks": asks,
        }
    except Exception as e:
        empty["status"] = "ERROR"
        empty["reason"] = f"Depth error: {type(e).__name__}: {str(e)[:120]}"
        return empty


def _depth_candidate_symbols(fyers, all_symbols, limit=30, extra_symbols=None):
    """Build depth universe without the old 50-symbol quotes bottleneck.

    If limit is 0, ALL NSE equity underlyings are returned. F&O stock
    underlyings can be merged through extra_symbols; duplicates are removed.
    For limited scans, current movement candidates are placed first, then the
    remaining NSE/F&O universe is appended.
    """
    universe = _validate_symbols(list(all_symbols or []) + list(extra_symbols or []))
    if not universe:
        return []

    requested_all = int(limit) == 0
    target = len(universe) if requested_all else max(1, min(int(limit), len(universe)))

    # Start with current movement candidates, but NEVER let a 50-row momentum
    # table or quotes API response cap an ALL-NSE scan.
    ordered = []
    seen = set()
    mdf = st.session_state.get("momentum_df")
    if isinstance(mdf, pd.DataFrame) and not mdf.empty and "Symbol" in mdf.columns:
        try:
            miter = mdf.sort_values("SCORE", ascending=False).iterrows() if "SCORE" in mdf.columns else mdf.iterrows()
            for _, row in miter:
                raw = str(row.get("Symbol", "")).strip().upper()
                sym = raw if raw.startswith("NSE:") else f"NSE:{raw}-EQ"
                if sym in universe and sym not in seen:
                    seen.add(sym)
                    ordered.append(sym)
        except Exception:
            pass

    # Append every remaining symbol. This is the important fix: ALL means ALL,
    # not the 50 symbols returned by FYERS quotes.
    for sym in universe:
        if sym not in seen:
            seen.add(sym)
            ordered.append(sym)

    if requested_all:
        return ordered
    return ordered[:target]

def run_live_depth_scan(fyers, symbols, max_workers=4):
    """Fetch real FYERS market-depth snapshots with a small worker pool."""

    symbols = _validate_symbols(symbols)
    rows, errors = [], []
    if not symbols:
        return pd.DataFrame(), ["No valid symbols for depth scan"]

    progress = st.progress(0.0, text=f"Live Order Book 0 / {len(symbols)}")
    done = 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(fetch_live_market_depth, fyers, s): s for s in symbols}
        for future in as_completed(futures):
            sym = futures[future]
            try:
                d = future.result()
                if d.get("status") == "OK":
                    ticker = sym.replace("NSE:", "").replace("-EQ", "")
                    rows.append({
                        "Symbol": ticker,
                        "LTP": d.get("ltp"),
                        "BEST BID": d.get("best_bid"),
                        "BEST ASK": d.get("best_ask"),
                        "BID QTY (5L)": round(d.get("bid_qty", 0), 0),
                        "ASK QTY (5L)": round(d.get("ask_qty", 0), 0),
                        "TOTAL BUY QTY": round(d.get("total_buy_qty", 0), 0),
                        "TOTAL SELL QTY": round(d.get("total_sell_qty", 0), 0),
                        "DEPTH IMBALANCE %": d.get("depth_imbalance", 0),
                        "BUY SCORE": d.get("buy_score", 0),
                        "SELL SCORE": d.get("sell_score", 0),
                        "NEXT DIRECTION": d.get("direction", "WAIT"),
                        "DIRECTION STRENGTH %": d.get("strength", 0),
                        "PIN SIGNAL": d.get("pin", "WAIT"),
                        "SPREAD": d.get("spread"),
                        "SPREAD %": d.get("spread_pct"),
                        "REASON": d.get("reason", ""),
                    })
                else:
                    errors.append(f"{sym}: {d.get('reason', 'No depth data')}")
            except Exception as e:
                errors.append(f"{sym}: {type(e).__name__}: {str(e)[:120]}")
            done += 1
            progress.progress(done / max(len(symbols), 1), text=f"Live Order Book {done} / {len(symbols)}")
    progress.empty()

    df = pd.DataFrame(rows)
    if not df.empty:
        df["__sort"] = pd.to_numeric(df["DIRECTION STRENGTH %"], errors="coerce").fillna(0)
        df = df.sort_values("__sort", ascending=False).drop(columns=["__sort"]).reset_index(drop=True)
    return df, errors



# ════════════════════════════════════════════════════════════════════════════════
# MULTI-FACTOR FUTURE DIRECTION ENGINE
# ════════════════════════════════════════════════════════════════════════════════
def _future_direction_engine(depth_row: Dict[str, Any], momentum_row: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Estimate the next directional bias from independent live/technical factors.

    This is a confidence score, NOT a guaranteed future-price prediction.
    Factors: order-book depth, momentum score/direction, PRE-MOVE, RVOL, structure,
    VWAP context (when available), and breakout/breakdown proximity.
    """
    buy = 0.0
    sell = 0.0
    buy_reasons, sell_reasons = [], []

    d = depth_row or {}
    m = momentum_row or {}

    # 1) Live order-book pressure: strongest live factor.
    imb = _safe_float(d.get("DEPTH IMBALANCE %", d.get("depth_imbalance", 0)))
    if imb >= 35:
        buy += 32; buy_reasons.append(f"strong bid depth +{imb:.1f}%")
    elif imb >= 15:
        buy += 20; buy_reasons.append(f"bid depth +{imb:.1f}%")
    elif imb <= -35:
        sell += 32; sell_reasons.append(f"strong ask depth {imb:.1f}%")
    elif imb <= -15:
        sell += 20; sell_reasons.append(f"ask depth {imb:.1f}%")

    # 2) Existing intraday momentum engine.
    md = str(m.get("DIRECTION", "")).upper()
    ms = _safe_float(m.get("SCORE", 0))
    if md in ("BUY", "UP"):
        pts = min(22.0, max(0.0, ms * 0.22))
        buy += pts; buy_reasons.append(f"momentum {ms:.0f}")
    elif md in ("SELL", "DOWN"):
        pts = min(22.0, max(0.0, ms * 0.22))
        sell += pts; sell_reasons.append(f"momentum {ms:.0f}")

    # 3) PRE-MOVE directional bias.
    pm = str(m.get("PRE-MOVE", "")).upper()
    pms = _safe_float(m.get("PRE-MOVE SCORE", 0))
    gap = abs(_safe_float(m.get("PRE SCORE GAP", 0)))
    if pm == "BUY":
        pts = min(20.0, pms * 0.16 + min(gap, 20) * 0.20)
        buy += pts; buy_reasons.append(f"pre-move BUY {pms:.0f}")
    elif pm == "SELL":
        pts = min(20.0, pms * 0.16 + min(gap, 20) * 0.20)
        sell += pts; sell_reasons.append(f"pre-move SELL {pms:.0f}")

    # 4) Volume participation / RVOL.
    rvol = _safe_float(m.get("RVOL", m.get("PRE-MOVE RVOL", 0)))
    if rvol >= 2.0:
        if buy > sell: buy += 10; buy_reasons.append(f"RVOL {rvol:.2f}")
        elif sell > buy: sell += 10; sell_reasons.append(f"RVOL {rvol:.2f}")
    elif rvol >= 1.5:
        if buy > sell: buy += 6; buy_reasons.append(f"RVOL {rvol:.2f}")
        elif sell > buy: sell += 6; sell_reasons.append(f"RVOL {rvol:.2f}")

    # 5) Structure.
    structure = str(m.get("STRUCTURE", "")).upper()
    if structure in ("HH/HL", "BULLISH", "UP") or str(m.get("HH/HL", "")) == "✅":
        buy += 8; buy_reasons.append("HH/HL structure")
    if structure in ("LH/LL", "BEARISH", "DOWN") or str(m.get("LH/LL", "")) == "✅":
        sell += 8; sell_reasons.append("LH/LL structure")

    # 6) Breakout / breakdown proximity from the PRE-MOVE engine.
    ltp = _safe_float(d.get("LTP", 0))
    bo = _safe_float(m.get("BREAKOUT LEVEL", 0))
    bd = _safe_float(m.get("BREAKDOWN LEVEL", 0))
    if ltp > 0 and bo > 0:
        dist = abs(bo - ltp) / ltp * 100
        if ltp <= bo and dist <= 0.35:
            buy += 8; buy_reasons.append(f"near breakout {dist:.2f}%")
    if ltp > 0 and bd > 0:
        dist = abs(ltp - bd) / ltp * 100
        if ltp >= bd and dist <= 0.35:
            sell += 8; sell_reasons.append(f"near breakdown {dist:.2f}%")

    buy = min(100.0, buy); sell = min(100.0, sell)
    gap = abs(buy - sell)
    lead = max(buy, sell)

    # Require multi-factor agreement; conflicting evidence becomes WAIT.
    if lead < 42 or gap < 12:
        direction = "WAIT"
        confidence = max(50.0, min(69.0, 50.0 + gap))
        label = "WAIT"
    elif buy > sell:
        direction = "STRONG BUY" if buy >= 72 and gap >= 22 else "BUY"
        confidence = min(95.0, 50.0 + gap * 1.45 + max(0.0, buy - 55) * 0.25)
        label = direction
    else:
        direction = "STRONG SELL" if sell >= 72 and gap >= 22 else "SELL"
        confidence = min(95.0, 50.0 + gap * 1.45 + max(0.0, sell - 55) * 0.25)
        label = direction

    reasons = buy_reasons if buy > sell else sell_reasons
    if direction == "WAIT":
        reasons = list(dict.fromkeys(buy_reasons + sell_reasons))[:4] or ["insufficient multi-factor agreement"]
    return {
        "direction": label,
        "confidence": round(confidence, 1),
        "buy_score": round(buy, 1),
        "sell_score": round(sell, 1),
        "reason": " | ".join(reasons),
    }


def _apply_future_direction_engine(depth_df: pd.DataFrame) -> pd.DataFrame:
    """Add multi-factor future-direction columns to live depth output."""
    if depth_df is None or depth_df.empty:
        return depth_df
    out = depth_df.copy()
    mdf = st.session_state.get("momentum_df")
    lookup = {}
    if isinstance(mdf, pd.DataFrame) and not mdf.empty and "Symbol" in mdf.columns:
        for _, r in mdf.iterrows():
            lookup[str(r.get("Symbol", "")).strip().upper()] = r.to_dict()

    results = []
    for _, row in out.iterrows():
        sym = str(row.get("Symbol", "")).strip().upper()
        results.append(_future_direction_engine(row.to_dict(), lookup.get(sym, {})))
    out["FUTURE DIRECTION"] = [x["direction"] for x in results]
    out["FUTURE CONFIDENCE %"] = [x["confidence"] for x in results]
    out["FUTURE BUY SCORE"] = [x["buy_score"] for x in results]
    out["FUTURE SELL SCORE"] = [x["sell_score"] for x in results]
    out["FUTURE REASON"] = [x["reason"] for x in results]
    return out

def _show_live_order_flow_tab(fyers, all_symbols):
    """Actual FYERS exchange depth tab: current bid/ask book -> direction -> PIN."""
    fo_symbols = st.session_state.get("fo_symbols", [])
    st.markdown("### 📖 LIVE EXCHANGE ORDER BOOK — NEXT DIRECTION + PIN")
    st.caption("Actual FYERS market-depth snapshot: bid/ask quantities and 5 visible depth levels. No TradingView footprint and no simulated order book.")
    st.caption(f"🇮🇳 NSE stocks: {len(all_symbols)} | 📈 F&O stock underlyings: {len(fo_symbols)} | 🔗 Combined unique: {len(_validate_symbols(list(all_symbols) + list(fo_symbols)))}")

    c1, c2, c3 = st.columns(3)
    with c1:
        depth_limit = st.number_input(
            "Depth candidates (0 = ALL NSE)",
            min_value=0, max_value=max(1, len(all_symbols)),
            value=min(DEPTH_SCAN_LIMIT_DEFAULT, max(1, len(all_symbols))),
            step=50, key="depth_scan_limit",
            help="FYERS REST depth is one symbol per request. ALL can be slow and may hit API rate limits."
        )
    with c2:
        st.metric("Depth API", "FYERS LIVE")
    with c3:
        st.metric("NSE + F&O Available", len(_validate_symbols(list(all_symbols) + list(fo_symbols))))

    depth_universe_limit = 0 if depth_limit == 0 else depth_limit
    candidates = _depth_candidate_symbols(fyers, all_symbols, depth_universe_limit, extra_symbols=st.session_state.get("fo_symbols", []))
    if st.session_state.get("momentum_df") is not None and not st.session_state.get("momentum_df").empty and depth_limit != 0:
        st.info(f"Using current movement candidates first: {len(candidates)} symbols.")
    else:
        st.info(
            f"Live depth candidates: {len(candidates)} / {len(_validate_symbols(list(all_symbols) + list(fo_symbols)))} NSE + F&O stock underlyings. "
            "FYERS REST depth is one symbol per request, so large scans can take longer."
        )

    if st.button("📖 SCAN LIVE EXCHANGE ORDER BOOK", key="depth_run", type="primary", use_container_width=True):
        with st.spinner(f"Reading live FYERS bid/ask market depth for {len(candidates)} symbols…"):
            depth_df, depth_errors = run_live_depth_scan(fyers, candidates)
        depth_df = _apply_future_direction_engine(depth_df)
        st.session_state["depth_df"] = depth_df
        st.session_state["depth_errors"] = depth_errors
        st.session_state["depth_scanned_at"] = _generated_timestamp()
        st.session_state["depth_scan_count"] = len(candidates)

    depth_df = st.session_state.get("depth_df")
    if isinstance(depth_df, pd.DataFrame) and not depth_df.empty:
        buy_df = depth_df[depth_df["FUTURE DIRECTION"].astype(str).str.contains("BUY", na=False)].copy()
        sell_df = depth_df[depth_df["FUTURE DIRECTION"].astype(str).str.contains("SELL", na=False)].copy()
        pins = depth_df[depth_df["PIN SIGNAL"].astype(str).str.contains("PIN", na=False)].copy()

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("DEPTH STOCKS", len(depth_df))
        m2.metric("🟢 BUY", len(buy_df))
        m3.metric("🔴 SELL", len(sell_df))
        m4.metric("📌 PINS", len(pins))
        st.caption(f"Last depth scan: {st.session_state.get('depth_scanned_at', 'N/A')}")

        st.markdown("### 🎯 NEXT DIRECTION — LIVE DEPTH")
        st.dataframe(
            depth_df[[
                "Symbol", "LTP", "BEST BID", "BEST ASK", "TOTAL BUY QTY", "TOTAL SELL QTY",
                "DEPTH IMBALANCE %", "BUY SCORE", "SELL SCORE", "NEXT DIRECTION",
                "DIRECTION STRENGTH %", "FUTURE DIRECTION", "FUTURE CONFIDENCE %",
                "FUTURE BUY SCORE", "FUTURE SELL SCORE", "PIN SIGNAL", "SPREAD %", "REASON", "FUTURE REASON"
            ]],
            use_container_width=True, height=500
        )

        if not pins.empty:
            st.markdown("### 📌 LIVE DEPTH PIN SIGNALS")
            st.dataframe(
                pins[["Symbol", "LTP", "TOTAL BUY QTY", "TOTAL SELL QTY", "DEPTH IMBALANCE %", "NEXT DIRECTION", "DIRECTION STRENGTH %", "PIN SIGNAL", "REASON"]],
                use_container_width=True, height=300
            )

        _excel_download_button(depth_df, "LIVE_ORDER_BOOK", "depth_excel", label="📥 DOWNLOAD LIVE ORDER BOOK EXCEL")
    elif "depth_df" in st.session_state:
        st.warning("No live depth rows were returned. Check FYERS market-data permissions and market hours.")
    else:
        st.info("👈 First run LIVE MOVEMENT, then scan LIVE EXCHANGE ORDER BOOK for the current bid/ask pressure.")

    errors = st.session_state.get("depth_errors", [])
    if errors:
        with st.expander(f"⚠️ Depth API errors ({len(errors)})", expanded=False):
            st.dataframe(pd.DataFrame({"Error": errors}), use_container_width=True)




def _pin_selected_tf_worker(fyers, symbol, resolution: str, tf_label: str):
    """Analyze one symbol on the timeframe selected in the PIN tab."""
    display_symbol = _pin_display_symbol(symbol)
    try:
        analysis = analyze_timeframe(fyers, symbol, resolution)
        df = analysis.get("df")
        data = analysis.get("data", {}) or {}
        if analysis.get("status") != "OK" or df is None or len(df) < 30:
            return None, f"{display_symbol}: {tf_label} unavailable/insufficient candles"

        # calculate_pin_rules is candle/timeframe agnostic internally.  The first
        # dataframe/data arguments are the active timeframe selected by the user.
        pin = calculate_pin_rules(df, data, {}, {})
        sweep = str(pin.get("SWEEP", "NONE")).upper()
        reversal = str(pin.get("REVERSAL", "NONE")).upper()

        buy_liq_signal = "WAIT"
        sell_liq_signal = "WAIT"
        if "LOW SWEPT" in sweep:
            buy_liq_signal = "🔥 STRONG BUY LIQ" if "BULL REVERSAL" in reversal else "🟢 BUY LIQ"
        if "HIGH SWEPT" in sweep:
            sell_liq_signal = "🔥 STRONG SELL LIQ" if "BEAR REVERSAL" in reversal else "🔴 SELL LIQ"

        if buy_liq_signal != "WAIT":
            final_liq_signal = buy_liq_signal
        elif sell_liq_signal != "WAIT":
            final_liq_signal = sell_liq_signal
        else:
            final_liq_signal = "⚪ WAIT"

        pin["Symbol"] = display_symbol
        pin["LTP"] = round(float(df["Close"].iloc[-1]), 2)
        pin["TIMEFRAME"] = tf_label
        pin["BUY LIQ SIGNAL"] = buy_liq_signal
        pin["SELL LIQ SIGNAL"] = sell_liq_signal
        pin["LIQUIDITY SIGNAL"] = final_liq_signal
        pin["TF TREND"] = data.get("structure_trend", "N/A")
        if "Time" in df.columns:
            try:
                pin["SIGNAL TIME"] = pd.Timestamp(df["Time"].iloc[-1]).strftime("%d-%b-%Y %H:%M")
            except Exception:
                pin["SIGNAL TIME"] = str(df["Time"].iloc[-1])
        else:
            pin["SIGNAL TIME"] = _now_ist().strftime("%d-%b-%Y %H:%M")
        return pin, None
    except Exception as e:
        return None, f"{display_symbol}: {type(e).__name__}: {str(e)[:100]}"


def _run_pin_selected_timeframe_scan(fyers, universe, resolution: str, tf_label: str,
                                     pin_min=PIN_MIN_CONFIDENCE, pin_mode="ALL"):
    """PIN scan for NSE/F&O on user-selected 5M, 15M or Daily candles."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    seen, symbols = set(), []
    for symbol in list(universe or []):
        normalized = _pin_normalize_symbol(symbol)
        if normalized and normalized not in seen:
            seen.add(normalized)
            symbols.append(normalized)
    if not symbols:
        return pd.DataFrame(), ["No valid symbols available"]

    rows, errors = [], []
    progress = st.progress(0.0, text=f"PIN {tf_label}: 0 / {len(symbols)}")
    with ThreadPoolExecutor(max_workers=PIN_FAST_WORKERS) as executor:
        futures = {
            executor.submit(_pin_selected_tf_worker, fyers, symbol, resolution, tf_label): symbol
            for symbol in symbols
        }
        for done, future in enumerate(as_completed(futures), start=1):
            try:
                row, error = future.result()
            except Exception as e:
                row, error = None, f"{futures[future]}: worker error: {str(e)[:100]}"
            if row is not None:
                rows.append(row)
            if error:
                errors.append(error)
            progress.progress(done / max(len(symbols), 1), text=f"PIN {tf_label}: {done} / {len(symbols)} | Results: {len(rows)}")
    progress.empty()

    result = pd.DataFrame(rows)
    if result.empty:
        return result, errors

    score = pd.to_numeric(result.get("PIN SCORE", 0), errors="coerce").fillna(0)
    liq_signal = result.get("LIQUIDITY SIGNAL", pd.Series("⚪ WAIT", index=result.index)).astype(str).str.upper()
    # Keep confirmed liquidity sweep signals even if the general PIN score is below
    # the slider; otherwise use the normal PIN score threshold.
    result = result[(score >= float(pin_min)) | liq_signal.str.contains("BUY LIQ|SELL LIQ", regex=True, na=False)].copy()

    if not result.empty:
        pin_text = result.get("PIN SIGNAL", pd.Series("", index=result.index)).astype(str).str.upper()
        liq_text = result.get("LIQUIDITY SIGNAL", pd.Series("", index=result.index)).astype(str).str.upper()
        combined = pin_text + " " + liq_text
        if pin_mode == "BUY ONLY":
            result = result[combined.str.contains("BUY", na=False)]
        elif pin_mode == "SELL ONLY":
            result = result[combined.str.contains("SELL", na=False)]
        elif pin_mode == "STRONG ONLY":
            result = result[combined.str.contains("STRONG", na=False)]

    if not result.empty:
        result["__liq_rank"] = result["LIQUIDITY SIGNAL"].astype(str).str.contains("STRONG", na=False).astype(int) * 2 + result["LIQUIDITY SIGNAL"].astype(str).str.contains("BUY LIQ|SELL LIQ", regex=True, na=False).astype(int)
        result["__score"] = pd.to_numeric(result.get("PIN SCORE", 0), errors="coerce").fillna(0)
        result = result.sort_values(["__liq_rank", "__score"], ascending=[False, False]).drop(columns=["__liq_rank", "__score"]).reset_index(drop=True)
    return result, errors

def _show_pin_rules_tab(fyers, all_symbols=None, fo_symbols=None) -> None:
    """PIN Rules scanner with NSE/F&O + 5M/15M/Daily selection."""
    st.markdown("### 📌 PIN RULES — BUY / SELL LIQUIDITY SIGNALS")
    st.caption("Select NSE or F&O, select 5 Minute / 15 Minute / 1 Day, then RUN. Existing scanner tabs are unchanged.")

    c_src, c_tf, c_count = st.columns(3)
    with c_src:
        source = st.selectbox("Market", ["NSE Stocks", "F&O Stocks"], key="pin_source")
    with c_tf:
        tf_label = st.selectbox("Timeframe", ["5 Minute", "15 Minute", "1 Day"], key="pin_timeframe")
    with c_count:
        selected_universe = list(all_symbols or []) if source == "NSE Stocks" else list(fo_symbols or [])
        st.metric("AVAILABLE", f"{len(selected_universe):,}")

    if not selected_universe:
        st.error(f"❌ No {source} symbols are available. Check symbol loading/FYERS access.")
        return

    resolution = {"5 Minute": "5", "15 Minute": "15", "1 Day": "1D"}[tf_label]

    with st.expander("📖 Liquidity Signal Rules", expanded=False):
        st.markdown("""
        **🟢 BUY LIQ SIGNAL:** selected-timeframe candle sweeps below confirmed sell-side liquidity and closes back above it.

        **🔴 SELL LIQ SIGNAL:** selected-timeframe candle sweeps above confirmed buy-side liquidity and closes back below it.

        **🔥 STRONG:** liquidity sweep also satisfies the existing reversal confirmation (candle direction + VWAP + RSI).

        Liquidity levels/signals are candle-based technical evidence, not exchange order-book quantity and not a guaranteed future move.
        """)

    candidate_symbols, seen = [], set()
    for symbol in selected_universe:
        normalized = _pin_normalize_symbol(symbol)
        if normalized and normalized not in seen:
            seen.add(normalized)
            candidate_symbols.append(normalized)
    total_available = len(candidate_symbols)
    if total_available == 0:
        st.warning("⚠️ No valid symbols found for PIN scanning.")
        return

    c1, c2, c3 = st.columns(3)
    with c1:
        pin_min = st.slider("Minimum PIN score", 50, 100, PIN_MIN_CONFIDENCE, 1, key="pin_min_score")
    with c2:
        pin_mode = st.selectbox("Show", ["ALL", "BUY ONLY", "SELL ONLY", "STRONG ONLY"], key="pin_mode")
    with c3:
        st.metric("TO SCAN", f"{total_available:,}")

    st.info(f"📌 PIN-{('F&O' if source == 'F&O Stocks' else 'NSE')}-{tf_label.upper()} | {total_available:,} symbols")

    run_label = f"▶ RUN PIN — {'F&O' if source == 'F&O Stocks' else 'NSE'} — {tf_label.upper()}"
    if st.button(run_label, key="pin_run", type="primary", use_container_width=True):
        with st.spinner(f"Scanning {source} {tf_label} liquidity signals…"):
            result, errors = _run_pin_selected_timeframe_scan(
                fyers, candidate_symbols, resolution, tf_label,
                pin_min=pin_min, pin_mode=pin_mode
            )
        st.session_state["pin_df"] = result
        st.session_state["pin_errors"] = errors
        st.session_state["pin_last_scan_count"] = total_available
        st.session_state["pin_last_source"] = source
        st.session_state["pin_last_timeframe"] = tf_label
        st.session_state["pin_scanned_at"] = _now_ist().strftime("%d-%b-%Y %H:%M:%S IST")

    pin_df = st.session_state.get("pin_df")
    if isinstance(pin_df, pd.DataFrame) and not pin_df.empty:
        liq_series = pin_df.get("LIQUIDITY SIGNAL", pd.Series("", index=pin_df.index)).astype(str).str.upper()
        pc1, pc2, pc3, pc4 = st.columns(4)
        pc1.metric("📌 RESULTS", len(pin_df))
        pc2.metric("🟢 BUY LIQ", int(liq_series.str.contains("BUY LIQ", na=False).sum()))
        pc3.metric("🔴 SELL LIQ", int(liq_series.str.contains("SELL LIQ", na=False).sum()))
        pc4.metric("🔥 STRONG LIQ", int(liq_series.str.contains("STRONG", na=False).sum()))
        st.caption(f"Last scan: {st.session_state.get('pin_last_source','')} | {st.session_state.get('pin_last_timeframe','')} | {st.session_state.get('pin_scanned_at','N/A')}")

        display_cols = [
            "Symbol", "TIMEFRAME", "LTP", "LIQUIDITY SIGNAL", "BUY LIQ SIGNAL", "SELL LIQ SIGNAL",
            "TOP LIQUIDITY LEVEL", "TOP LIQUIDITY STATUS", "BOTTOM LIQUIDITY LEVEL", "BOTTOM LIQUIDITY STATUS",
            "BUY LIQUIDITY", "BUY LIQUIDITY SCORE", "SELL LIQUIDITY", "SELL LIQUIDITY SCORE",
            "SWEEP", "REVERSAL", "EQUAL HIGH", "EQUAL LOW", "TF TREND", "RVOL", "RSI",
            "PIN SIGNAL", "PIN SCORE", "BIG MOVEMENT", "BIG MOVE SCORE", "SIGNAL TIME", "REASON"
        ]
        display_cols = [c for c in display_cols if c in pin_df.columns]
        st.dataframe(pin_df[display_cols], use_container_width=True, height=540, hide_index=True)

        try:
            safe_tf = st.session_state.get("pin_last_timeframe", "TF").replace(" ", "_")
            safe_src = "FO" if st.session_state.get("pin_last_source") == "F&O Stocks" else "NSE"
            st.download_button(
                "📥 DOWNLOAD PIN LIQUIDITY EXCEL",
                _format_excel_output(pin_df, "PIN_LIQUIDITY"),
                f"PIN_{safe_src}_{safe_tf}_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key="pin_excel", use_container_width=True,
            )
        except Exception as e:
            st.error(f"❌ PIN Excel export failed: {str(e)[:150]}")
    elif "pin_df" in st.session_state:
        st.warning("⚠️ No stocks matched the selected PIN/liquidity rules.")

    errors = st.session_state.get("pin_errors", [])
    if errors:
        with st.expander(f"⚠️ {len(errors)} symbols could not be analyzed", expanded=False):
            for err in errors[:100]:
                st.write(f"• {err}")
            if len(errors) > 100:
                st.caption(f"Showing first 100 of {len(errors)} errors.")

# ════════════════════════════════════════════════════════════════════════════════
# REVERSAL REPORT ENGINE — uses stocks already scanned in NSE / F&O / MOMENTUM / SWING
# and calculates a practical reversal watch-zone from the latest available scan values.
def _add_reversal_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Attach a conservative reversal watch signal/score/zone to an existing scan report.

    A normal BUY/SELL momentum signal is not reversal evidence. Reversal needs
    opposing context plus oversold/overbought, sweep/rejection, VWAP or volume
    confirmation, so ordinary BUY/SELL rows do not get mislabeled as reversals.
    """
    if not isinstance(df, pd.DataFrame) or df.empty:
        return df
    x = df.copy()
    def num(names, default=0.0):
        for name in names:
            if name in x.columns:
                return pd.to_numeric(x[name], errors="coerce").fillna(default)
        return pd.Series(default, index=x.index, dtype=float)
    ltp = num(["LTP", "ltp"], np.nan)
    rsi = num(["RSI", "rsi"], 50)
    rvol = num(["RVOL", "rvol", "PRE-MOVE RVOL"], 1)
    conf = num(["AI CONFIDENCE %", "BEFORE MOVE SCORE", "SCORE", "score"], 0)
    atr = num(["ATR", "atr"], 0).where(lambda s: s > 0, ltp.abs() * 0.01)
    vwap = num(["VWAP", "vwap"], 0)
    valid_vwap = (vwap > 0) & ltp.notna()
    text = pd.Series("", index=x.index, dtype=str)
    for col in ["BEFORE MOVE DIRECTION", "PRE-MOVE", "DIRECTION", "MOVEMENT STATUS", "AI SIGNAL", "Signal", "trend", "REVERSAL", "SWEEP", "REASON", "reason"]:
        if col in x.columns:
            text = text + " " + x[col].astype(str).str.upper()
    down_context = text.str.contains("DOWN|BEAR|SELL BEFORE MOVE|PRE-MOVE SELL|LOW SWEPT|BULL REVERSAL|BULLISH SWEEP|SELL REVERSAL", regex=True, na=False)
    up_context = text.str.contains("UP|BULL|BUY BEFORE MOVE|PRE-MOVE BUY|HIGH SWEPT|BEAR REVERSAL|BEARISH SWEEP|BUY REVERSAL", regex=True, na=False)
    bull_rejection = text.str.contains("LOW SWEPT|BULL REVERSAL|BULLISH SWEEP|BULLISH REJECTION|HAMMER", regex=True, na=False)
    bear_rejection = text.str.contains("HIGH SWEPT|BEAR REVERSAL|BEARISH SWEEP|BEARISH REJECTION|SHOOTING STAR", regex=True, na=False)
    buy = ((rsi <= 35).astype(int) + down_context.astype(int) + bull_rejection.astype(int) + ((valid_vwap) & (ltp >= vwap)).astype(int) + ((rvol >= 1.30) & down_context).astype(int))
    sell = ((rsi >= 65).astype(int) + up_context.astype(int) + bear_rejection.astype(int) + ((valid_vwap) & (ltp <= vwap)).astype(int) + ((rvol >= 1.30) & up_context).astype(int))
    buy_ready = (buy >= 3) & (buy > sell) & down_context
    sell_ready = (sell >= 3) & (sell > buy) & up_context
    direction = np.where(buy_ready, "BUY REVERSAL", np.where(sell_ready, "SELL REVERSAL", "WAIT"))
    x["REVERSAL SIGNAL"] = direction
    raw_rev_score = np.maximum(buy, sell) * 18 + (rvol >= 1.30).astype(int) * 8 + (conf >= 70).astype(int) * 5
    x["REVERSAL SCORE"] = np.where(np.array(direction) == "WAIT", np.minimum(raw_rev_score, 59), np.minimum(raw_rev_score, 100)).round(1)
    # WAIT rows must NOT display LTP as a fake reversal price.
    # Prefer the existing breakout/breakdown reference when available.
    breakout_level = num(["BREAKOUT LEVEL", "breakout_level"], np.nan)
    breakdown_level = num(["BREAKDOWN LEVEL", "breakdown_level"], np.nan)

    buy_reversal_price = np.where(
        breakdown_level.notna() & (breakdown_level > 0),
        breakdown_level + (atr * 0.20),
        ltp - (atr * 0.50)
    )
    sell_reversal_price = np.where(
        breakout_level.notna() & (breakout_level > 0),
        breakout_level - (atr * 0.20),
        ltp + (atr * 0.50)
    )

    # REVERSAL LEVEL should always be a usable reference price.
    # Even when REVERSAL SIGNAL = WAIT, do not show N/A.
    # Use the prevailing direction/context to choose the correct side.
    dir_text = x["DIRECTION"].astype(str).str.upper() if "DIRECTION" in x.columns else pd.Series("", index=x.index)

    reversal_level = np.where(
        np.array(direction) == "BUY REVERSAL",
        buy_reversal_price,
        np.where(
            np.array(direction) == "SELL REVERSAL",
            sell_reversal_price,
            np.where(
                dir_text.str.contains("SELL", na=False),
                np.where(
                    breakdown_level.notna() & (breakdown_level > 0),
                    breakdown_level + (atr * 0.20),
                    ltp + (atr * 0.50)
                ),
                np.where(
                    breakout_level.notna() & (breakout_level > 0),
                    breakout_level - (atr * 0.20),
                    ltp - (atr * 0.50)
                )
            )
        )
    )

    # Final fallback: if any price is still invalid, use ATR-based reference,
    # never LTP itself and never N/A.
    # IMPORTANT: np.where() returns a NumPy ndarray.
    # Convert it back to a pandas Series before using .fillna().
    reversal_level = pd.Series(
        pd.to_numeric(reversal_level, errors="coerce"),
        index=x.index,
        dtype="float64"
    )

    fallback_level = pd.Series(
        np.where(
            dir_text.str.contains("SELL", na=False),
            ltp + (atr * 0.50),
            ltp - (atr * 0.50)
        ),
        index=x.index,
        dtype="float64"
    )

    reversal_level = reversal_level.fillna(fallback_level)
    x["REVERSAL LEVEL"] = reversal_level.round(2)

    x["REVERSAL ZONE"] = np.where(
        dir_text.str.contains("SELL", na=False),
        (x["REVERSAL LEVEL"] - atr * 0.25).round(2).astype(str) + " - " +
        (x["REVERSAL LEVEL"] + atr * 0.50).round(2).astype(str),
        (x["REVERSAL LEVEL"] - atr * 0.50).round(2).astype(str) + " - " +
        (x["REVERSAL LEVEL"] + atr * 0.25).round(2).astype(str)
    )
    x["REVERSAL REASON"] = np.where(np.array(direction) == "BUY REVERSAL", "Down context + oversold/rejection + VWAP/volume confirmation", np.where(np.array(direction) == "SELL REVERSAL", "Up context + overbought/rejection + VWAP/volume confirmation", "No 3-factor reversal confluence"))
    return x



# ════════════════════════════════════════════════════════════════════════════════
# AI CHART ANALYSIS — ADDITIONAL TAB ONLY
# ════════════════════════════════════════════════════════════════════════════════
def _ai_chart_signal(analysis_5m: Dict[str, Any],
                     analysis_15m: Dict[str, Any],
                     analysis_1h: Dict[str, Any]) -> Dict[str, Any]:
    """Rule-based AI-style chart synthesis using the scanner's existing indicators.
    This does not claim to predict the future with certainty.
    """
    d5 = analysis_5m.get("data", {}) or {}
    d15 = analysis_15m.get("data", {}) or {}
    d1h = analysis_1h.get("data", {}) or {}
    if not d5:
        return {"direction": "WAIT", "confidence": 0.0, "buy_score": 0.0,
                "sell_score": 0.0, "reason": "5M data unavailable"}

    buy = 0.0
    sell = 0.0
    buy_reasons, sell_reasons = [], []

    def add_trend(data, weight):
        nonlocal buy, sell
        trend = str(data.get("structure_trend", "NEUTRAL")).upper()
        if trend == "BULLISH":
            buy += weight
            buy_reasons.append(f"Bullish {weight:.0f}x trend")
        elif trend == "BEARISH":
            sell += weight
            sell_reasons.append(f"Bearish {weight:.0f}x trend")

    add_trend(d5, 25)
    add_trend(d15, 20)
    add_trend(d1h, 15)

    # VWAP + EMA
    price = float(d5.get("last_close", 0) or 0)
    vwap = d5.get("vwap")
    if vwap is not None and price:
        if price > float(vwap):
            buy += 10; buy_reasons.append("Price above VWAP")
        elif price < float(vwap):
            sell += 10; sell_reasons.append("Price below VWAP")

    ema = str(d5.get("ema_trend", "NEUTRAL")).upper()
    if ema == "BULLISH":
        buy += 8; buy_reasons.append("EMA alignment bullish")
    elif ema == "BEARISH":
        sell += 8; sell_reasons.append("EMA alignment bearish")

    pressure = str(d5.get("pressure_trend", "NEUTRAL")).upper()
    if pressure == "STRONG_BUYING":
        buy += 10; buy_reasons.append("Strong buying pressure")
    elif pressure == "BUYING":
        buy += 6; buy_reasons.append("Buying pressure")
    elif pressure == "STRONG_SELLING":
        sell += 10; sell_reasons.append("Strong selling pressure")
    elif pressure == "SELLING":
        sell += 6; sell_reasons.append("Selling pressure")

    rvol = float(d5.get("rvol", 1.0) or 1.0)
    if rvol >= 2.0:
        if buy >= sell:
            buy += 7; buy_reasons.append(f"High RVOL {rvol:.2f}x")
        else:
            sell += 7; sell_reasons.append(f"High RVOL {rvol:.2f}x")
    elif rvol >= 1.5:
        if buy >= sell:
            buy += 4; buy_reasons.append(f"RVOL {rvol:.2f}x")
        else:
            sell += 4; sell_reasons.append(f"RVOL {rvol:.2f}x")

    rsi = float(d5.get("rsi", 50) or 50)
    if 55 <= rsi < 70:
        buy += 5; buy_reasons.append(f"RSI {rsi:.1f} bullish zone")
    elif 30 < rsi <= 45:
        sell += 5; sell_reasons.append(f"RSI {rsi:.1f} bearish zone")

    if d5.get("macd_bullish"):
        buy += 5; buy_reasons.append("MACD bullish")
    else:
        sell += 5; sell_reasons.append("MACD bearish")

    # Confirmed structure events
    if d5.get("bullish_choch") or d5.get("bullish_mss") or d5.get("bullish_cisd"):
        buy += 8; buy_reasons.append("Bullish structure event")
    if d5.get("bearish_choch") or d5.get("bearish_mss") or d5.get("bearish_cisd"):
        sell += 8; sell_reasons.append("Bearish structure event")

    total = max(buy + sell, 1.0)
    gap = abs(buy - sell)
    confidence = min(98.0, 50.0 + (gap / total) * 48.0)

    if buy >= sell and gap >= 12:
        direction = "🟢 BUY"
        reasons = buy_reasons
    elif sell > buy and gap >= 12:
        direction = "🔴 SELL"
        reasons = sell_reasons
    else:
        direction = "🟡 WAIT"
        reasons = list(dict.fromkeys(buy_reasons + sell_reasons))[:5] or ["Mixed signals"]

    return {
        "direction": direction,
        "confidence": round(confidence, 1),
        "buy_score": round(buy, 1),
        "sell_score": round(sell, 1),
        "reason": " | ".join(reasons[:6]),
    }


def _plot_ai_candles(df: pd.DataFrame, ticker: str, signal: Dict[str, Any]):
    """Simple dependency-light OHLC chart with EMA/VWAP overlays."""
    if not MATPLOTLIB_AVAILABLE or df is None or df.empty:
        st.warning("Chart library/data unavailable.")
        return

    d = df.tail(100).copy().reset_index(drop=True)
    close = pd.to_numeric(d["Close"], errors="coerce")
    ema9 = close.ewm(span=9, adjust=False).mean()
    ema21 = close.ewm(span=21, adjust=False).mean()
    vwap = calculate_vwap(d)

    fig, ax = plt.subplots(figsize=(13, 5.5))
    for i, row in d.iterrows():
        o, h, l, c = map(float, [row["Open"], row["High"], row["Low"], row["Close"]])
        ax.vlines(i, l, h, linewidth=1)
        bottom = min(o, c)
        height = max(abs(c-o), max((h-l)*0.01, 0.000001))
        rect = plt.Rectangle((i-0.32, bottom), 0.64, height,
                             fill=False if c >= o else True, linewidth=1)
        ax.add_patch(rect)

    ax.plot(range(len(d)), ema9, linewidth=1.3, label="EMA 9")
    ax.plot(range(len(d)), ema21, linewidth=1.3, label="EMA 21")
    ax.plot(range(len(d)), vwap, linewidth=1.1, label="VWAP")

    ax.set_title(f"{ticker} — 5M AI Chart Analysis | {signal.get('direction','WAIT')} | {signal.get('confidence',0):.1f}%")
    ax.set_xlabel("Recent 5M candles")
    ax.set_ylabel("Price")
    ax.grid(alpha=0.2)
    ax.legend(loc="upper left")
    st.pyplot(fig, use_container_width=True)
    plt.close(fig)


def _analyze_submitted_chart_image(image_bytes: bytes) -> str:
    """Analyze ONLY the exact submitted chart image with an OpenAI vision model."""
    if not image_bytes:
        raise ValueError("No submitted chart image found.")

    # Reuse the existing Streamlit configuration first, with an environment
    # fallback so the addition works in normal Streamlit deployments too.
    try:
        api_key = str(st.secrets.get("OPENAI_API_KEY", "")).strip()
    except Exception:
        api_key = ""
    if not api_key:
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise ValueError(
            "OPENAI_API_KEY is required. Add OPENAI_API_KEY to Streamlit Secrets "
            "or the environment before submitting a chart."
        )

    try:
        model = str(st.secrets.get("OPENAI_MODEL", "")).strip()
    except Exception:
        model = ""
    model = model or os.environ.get("OPENAI_MODEL", "").strip() or "gpt-5.6-luna"

    import base64
    image_b64 = base64.b64encode(image_bytes).decode("ascii")
    mime = _chart_image_mime(image_bytes)

    prompt = """
You are an expert technical-chart analyst. Analyze ONLY the exact chart screenshot attached to this request.
The image is the sole source of truth. Do not use NSE/F&O selections, Select Stock values, FYERS data,
external market data, memory, or assumptions about the instrument.

ANTI-HALLUCINATION RULES:
1. Read instrument, timeframe, price and levels ONLY when they are visibly readable in the image.
2. If a requested value is not readable or not visibly derivable, return exactly "N/A". Never guess.
3. Support/resistance may be derived only from clearly visible price structure in the screenshot. If not clear, use N/A.
4. Liquidity/sweep and breakout/breakdown must be marked N/A unless clearly visible.
5. CALL/PUT bias must be based only on the visible chart. Do not invent an option strike, premium, expiry, or chain data.
6. Confidence is confidence in the screenshot interpretation, not a guarantee of future price movement.
7. If the screenshot is insufficient for a reliable setup, the final decision must be WAIT.

Return ONLY valid JSON with these exact keys and string/number values. Do not add markdown fences or extra text:
{
  "instrument": "...",
  "timeframe": "...",
  "current_visible_price": "...",
  "market_direction": "BULLISH / BEARISH / SIDEWAYS / UNCLEAR",
  "trend": "...",
  "support_levels": "...",
  "resistance_levels": "...",
  "breakout_breakdown": "...",
  "liquidity_sweep": "...",
  "call_put_bias": "CALL / PUT / NO TRADE / N/A",
  "entry_zone": "...",
  "stop_loss": "...",
  "target_1": "...",
  "target_2": "...",
  "risk_reward": "...",
  "scalping_setup": "...",
  "next_probable_move": "...",
  "confidence_percent": 0,
  "final_decision": "BUY / SELL / WAIT",
  "evidence": "...",
  "warning": "..."
}

For confidence_percent use a number from 0 to 100. If the screenshot does not support a reliable setup,
use a conservative confidence and final_decision WAIT.
""".strip()

    body = {
        "model": model,
        "input": [{
            "role": "user",
            "content": [
                {"type": "input_text", "text": prompt},
                {
                    "type": "input_image",
                    "image_url": f"data:{mime};base64,{image_b64}",
                },
            ],
        }],
    }

    response = requests.post(
        "https://api.openai.com/v1/responses",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=120,
    )

    if not response.ok:
        try:
            err = response.json()
        except Exception:
            err = response.text
        raise RuntimeError(
            f"OpenAI API error {response.status_code}: {str(err)[:700]}"
        )

    payload = response.json()
    report = _extract_openai_response_text(payload)
    if not report:
        raise RuntimeError("OpenAI returned an empty chart-analysis report.")
    return report


def _parse_chart_vision_report(report_text: str) -> Dict[str, Any]:
    """Parse the vision response JSON while remaining safe if the model adds fences."""
    text = (report_text or "").strip()
    candidates = [text]
    if text.startswith("```"):
        candidates.append(re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I | re.S).strip())

    for candidate in candidates:
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass

    # Last-resort extraction if a model surrounded JSON with prose.
    first, last = text.find("{"), text.rfind("}")
    if first >= 0 and last > first:
        try:
            obj = json.loads(text[first:last + 1])
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass

    return {
        "instrument": "N/A",
        "timeframe": "N/A",
        "current_visible_price": "N/A",
        "market_direction": "UNCLEAR",
        "trend": "N/A",
        "support_levels": "N/A",
        "resistance_levels": "N/A",
        "breakout_breakdown": "N/A",
        "liquidity_sweep": "N/A",
        "call_put_bias": "N/A",
        "entry_zone": "N/A",
        "stop_loss": "N/A",
        "target_1": "N/A",
        "target_2": "N/A",
        "risk_reward": "N/A",
        "scalping_setup": "N/A",
        "next_probable_move": "N/A",
        "confidence_percent": 0,
        "final_decision": "WAIT",
        "evidence": "The AI response could not be parsed safely; no chart values were inferred.",
        "warning": "Screenshot analysis is unavailable in structured form. Please re-analyze the chart.",
    }


def _show_ai_chart_analysis_tab():
    """AI Chart Analysis tab. Screenshot submission is always available and independent.

    The existing NSE/F&O stock analysis is retained below as a separate optional section.
    """
    st.markdown("### 🤖 AI CHART ANALYSIS")
    st.success(
        "📷 INDEPENDENT CHART MODE — Upload or paste a chart, then click "
        "🧠 SUBMIT CHART → ANALYZE. Only that exact image is sent to AI. "
        "NSE/F&O Universe, Select Stock and FYERS data are NOT used."
    )

    st.markdown("#### 📷 PASTE / UPLOAD CHART")
    st.caption(
        "Upload a chart screenshot or paste one from the clipboard. NIFTY, BANKNIFTY, stocks, "
        "crypto, commodities, etc. are accepted. Only the submitted image is analyzed."
    )

    uploaded_chart = st.file_uploader(
        "📁 Upload chart screenshot",
        type=["png", "jpg", "jpeg", "webp"],
        key="ai_chart_vision_upload_v3",
        help="The exact image selected here is sent to the AI vision model after submission.",
    )

    if PASTE_IMAGE_AVAILABLE:
        paste_result = paste_image_button(
            "📋 PASTE CHART SCREENSHOT",
            key="ai_chart_vision_paste_v3",
            errors="ignore",
        )
        if paste_result is not None and paste_result.image_data is not None:
            try:
                buf = io.BytesIO()
                paste_result.image_data.save(buf, format="PNG")
                pasted_bytes = buf.getvalue()
                st.session_state["ai_chart_vision_pasted_bytes_v3"] = pasted_bytes
                # A new pasted image is a new candidate, not an automatic submission.
                st.session_state["ai_chart_vision_source_hash_v3"] = None
            except Exception as e:
                st.error(f"❌ Clipboard paste failed: {e}")
    else:
        st.warning(
            "📋 Clipboard paste requires `streamlit-paste-button`. "
            "Install it with: `pip install streamlit-paste-button`. Upload still works without it."
        )

    pasted_chart_bytes = st.session_state.get("ai_chart_vision_pasted_bytes_v3")

    # Prefer a newly uploaded file over an older clipboard image.
    if uploaded_chart is not None:
        candidate_bytes = uploaded_chart.getvalue()
        candidate_source = "Uploaded chart"
    else:
        candidate_bytes = pasted_chart_bytes
        candidate_source = "Pasted chart"

    if candidate_bytes:
        import hashlib
        candidate_hash = hashlib.sha256(candidate_bytes).hexdigest()
        submitted_hash = st.session_state.get("ai_chart_vision_source_hash_v3")

        # If the user supplied a different image, clear only the screenshot report.
        if submitted_hash and candidate_hash != submitted_hash:
            st.session_state["ai_chart_vision_report_v3"] = None
            st.session_state["ai_chart_vision_submitted_bytes_v3"] = None
            st.session_state["ai_chart_vision_completed_v3"] = False

        st.image(
            candidate_bytes,
            caption=f"Preview — {candidate_source} (this exact image will be submitted)",
        )

    chart_ready = bool(candidate_bytes)
    submitted_report = st.session_state.get("ai_chart_vision_report_v3")
    completed = bool(st.session_state.get("ai_chart_vision_completed_v3") and submitted_report)

    if not chart_ready:
        st.warning("👆 Upload or paste a chart screenshot first.")

    # Required prominent submit button. It is disabled until an image exists.
    submit_label = "✅ CHART ANALYSIS COMPLETED" if completed else "🧠 SUBMIT CHART → ANALYZE"
    if st.button(
        submit_label,
        key="ai_chart_vision_submit_v3",
        type="primary",
        use_container_width=True,
        disabled=not chart_ready or completed,
    ):
        import hashlib
        exact_bytes = bytes(candidate_bytes)
        exact_hash = hashlib.sha256(exact_bytes).hexdigest()

        # Important: only these bytes are sent to the vision model. No symbol,
        # universe, Fyers data, or selected-stock value is passed to the request.
        st.session_state["ai_chart_vision_submitted_bytes_v3"] = exact_bytes
        st.session_state["ai_chart_vision_source_hash_v3"] = exact_hash
        st.session_state["ai_chart_vision_report_v3"] = None
        st.session_state["ai_chart_vision_completed_v3"] = False
        st.session_state["ai_chart_vision_submit_time_v3"] = _generated_timestamp()

        with st.spinner("🧠 AI vision is analyzing ONLY the submitted chart image…"):
            try:
                raw_report = _analyze_submitted_chart_image(exact_bytes)
                parsed_report = _parse_chart_vision_report(raw_report)
                st.session_state["ai_chart_vision_report_v3"] = parsed_report
                st.session_state["ai_chart_vision_raw_report_v3"] = raw_report
                st.session_state["ai_chart_vision_completed_v3"] = True
                st.rerun()
            except Exception as e:
                st.session_state["ai_chart_vision_completed_v3"] = False
                st.error(f"❌ Chart analysis failed: {type(e).__name__}: {str(e)[:1200]}")

    # Re-analysis intentionally reuses the already submitted image, not a stock.
    if st.session_state.get("ai_chart_vision_submitted_bytes_v3"):
        if st.button(
            "🔄 RE-ANALYZE CHART",
            key="ai_chart_vision_reanalyze_v3",
            use_container_width=True,
        ):
            exact_bytes = bytes(st.session_state["ai_chart_vision_submitted_bytes_v3"])
            st.session_state["ai_chart_vision_report_v3"] = None
            st.session_state["ai_chart_vision_completed_v3"] = False
            with st.spinner("🧠 Re-analyzing the exact submitted image…"):
                try:
                    raw_report = _analyze_submitted_chart_image(exact_bytes)
                    parsed_report = _parse_chart_vision_report(raw_report)
                    st.session_state["ai_chart_vision_report_v3"] = parsed_report
                    st.session_state["ai_chart_vision_raw_report_v3"] = raw_report
                    st.session_state["ai_chart_vision_completed_v3"] = True
                    st.rerun()
                except Exception as e:
                    st.error(f"❌ Re-analysis failed: {type(e).__name__}: {str(e)[:1200]}")

    report = st.session_state.get("ai_chart_vision_report_v3")
    submitted_bytes = st.session_state.get("ai_chart_vision_submitted_bytes_v3")
    if not report or not submitted_bytes:
        st.info("After submission, the chart analysis report will remain visible here across Streamlit reruns.")
        return

    st.markdown("---")
    st.markdown("## 📋 CHART ANALYSIS REPORT")
    st.image(
        submitted_bytes,
        caption=f"Exact submitted chart image • {st.session_state.get('ai_chart_vision_submit_time_v3', 'N/A')}",
    )

    def _r(key: str, default: str = "N/A") -> str:
        value = report.get(key, default)
        if value is None or str(value).strip() == "":
            return default
        return str(value)

    try:
        confidence = float(report.get("confidence_percent", 0))
    except Exception:
        confidence = 0.0
    confidence = max(0.0, min(100.0, confidence))

    decision = _r("final_decision", "WAIT").upper()
    direction = _r("market_direction", "UNCLEAR").upper()

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("INSTRUMENT", _r("instrument"))
    c2.metric("TIMEFRAME", _r("timeframe"))
    c3.metric("VISIBLE PRICE", _r("current_visible_price"))
    c4.metric("CONFIDENCE", f"{confidence:.0f}%")

    if decision == "BUY":
        st.success(f"🟢 FINAL DECISION: {decision}")
    elif decision == "SELL":
        st.error(f"🔴 FINAL DECISION: {decision}")
    else:
        st.warning(f"🟡 FINAL DECISION: {decision}")

    st.markdown("### 🧭 MARKET DIRECTION")
    st.write(f"**Market Direction:** {direction}")
    st.write(f"**Trend:** {_r('trend')}")

    st.markdown("### 🧱 SUPPORT / RESISTANCE")
    sr_df = pd.DataFrame([
        {
            "ITEM": "Support levels",
            "VALUE": _r("support_levels"),
        },
        {
            "ITEM": "Resistance levels",
            "VALUE": _r("resistance_levels"),
        },
        {
            "ITEM": "Breakout / Breakdown",
            "VALUE": _r("breakout_breakdown"),
        },
        {
            "ITEM": "Liquidity / Sweep",
            "VALUE": _r("liquidity_sweep"),
        },
    ])
    st.dataframe(sr_df, use_container_width=True, hide_index=True)

    st.markdown("### 🎯 TRADE SETUP")
    setup_df = pd.DataFrame([
        {"ITEM": "CALL / PUT bias", "VALUE": _r("call_put_bias")},
        {"ITEM": "Entry zone", "VALUE": _r("entry_zone")},
        {"ITEM": "Stop Loss", "VALUE": _r("stop_loss")},
        {"ITEM": "Target 1", "VALUE": _r("target_1")},
        {"ITEM": "Target 2", "VALUE": _r("target_2")},
        {"ITEM": "Risk / Reward", "VALUE": _r("risk_reward")},
        {"ITEM": "Scalping setup", "VALUE": _r("scalping_setup")},
    ])
    st.dataframe(setup_df, use_container_width=True, hide_index=True)

    st.markdown("### ⚡ NEXT PROBABLE MOVE")
    st.info(_r("next_probable_move"))

    st.markdown("### 🔍 CHART EVIDENCE")
    st.write(_r("evidence"))
    warning = _r("warning")
    if warning != "N/A":
        st.warning(warning)

    # Excel export uses only the screenshot report values. It does not include
    # or query the NSE/F&O selected stock.
    report_row = {
        "Chart / Instrument Identification": _r("instrument"),
        "Timeframe visible in chart": _r("timeframe"),
        "Current visible price": _r("current_visible_price"),
        "Market Direction": direction,
        "Trend": _r("trend"),
        "Support levels": _r("support_levels"),
        "Resistance levels": _r("resistance_levels"),
        "Breakout / Breakdown": _r("breakout_breakdown"),
        "Liquidity / Sweep": _r("liquidity_sweep"),
        "CALL / PUT bias": _r("call_put_bias"),
        "Entry zone": _r("entry_zone"),
        "Stop Loss": _r("stop_loss"),
        "Target 1": _r("target_1"),
        "Target 2": _r("target_2"),
        "Risk / Reward": _r("risk_reward"),
        "Scalping setup": _r("scalping_setup"),
        "Next probable move": _r("next_probable_move"),
        "Confidence %": confidence,
        "Final Buy / Sell / Wait decision": decision,
        "Evidence": _r("evidence"),
        "Warning": warning,
        "Submitted at": st.session_state.get("ai_chart_vision_submit_time_v3", "N/A"),
    }
    report_df = pd.DataFrame([report_row])
    st.markdown("### 📥 REPORT EXPORT")
    _excel_download_button(report_df, "CHART_VISION_ANALYSIS", "ai_chart_vision_excel_v3", label="📊 DOWNLOAD EXCEL REPORT")

    st.caption(
        "This report is based only on the submitted screenshot. Values that were not readable were returned as N/A. "
        "Educational analysis only; not a guarantee of market movement."
    )



def _show_big_movement_alert_box() -> None:
    """Show confirmed stock big-movement names in a blinking box across scanner tabs."""
    df = st.session_state.get("momentum_df")
    st.markdown("""
    <style>
    @keyframes bigMoveBlink { 0%, 48% { opacity: 1; } 49%, 100% { opacity: .42; } }
    .bigmove-alert-wrap { border: 1px solid #d94b4b; border-radius: 12px; padding: 12px 14px;
        margin: 8px 0 16px 0; background: rgba(130, 20, 20, 0.08); }
    .bigmove-alert-title { font-size: 18px; font-weight: 800; margin-bottom: 8px; }
    .bigmove-chip { display: inline-block; margin: 4px 7px 4px 0; padding: 7px 10px;
        border-radius: 8px; font-weight: 750; animation: bigMoveBlink 1.05s steps(2, start) infinite; }
    .bigmove-up { color: #b7ffd0; background: #14532d; border: 1px solid #22a05a; }
    .bigmove-down { color: #ffd0d0; background: #7f1d1d; border: 1px solid #ef4444; }
    .bigmove-empty { color: #777; font-size: 13px; }
    @media (prefers-reduced-motion: reduce) { .bigmove-chip { animation: none; } }
    </style>
    """, unsafe_allow_html=True)
    st.markdown('<div class="bigmove-alert-wrap"><div class="bigmove-alert-title">🚨 STOCK BIG MOVEMENT ALERT BOX</div>', unsafe_allow_html=True)
    if df is None or df.empty or "MOVEMENT STATUS" not in df.columns:
        st.markdown('<div class="bigmove-empty">Run SCAN INTRADAY MOVEMENT to show detected stock names here.</div></div>', unsafe_allow_html=True)
        return
    status = df["MOVEMENT STATUS"].astype(str).str.upper()
    confirmed = df[status.str.contains("BIG BUY|BIG SELL", regex=True, na=False)].copy()
    if confirmed.empty:
        st.markdown('<div class="bigmove-empty">No confirmed BIG BUY / BIG SELL signal in the last scan. This box will update after the next scan.</div></div>', unsafe_allow_html=True)
        return
    symbol_col = next((c for c in ["STOCK NAME", "SYMBOL", "Symbol", "symbol"] if c in confirmed.columns), None)
    if symbol_col is None:
        st.markdown('<div class="bigmove-empty">Confirmed movement found, but the stock-name column is unavailable.</div></div>', unsafe_allow_html=True)
        return
    confirmed = confirmed.head(20)
    chips = []
    for _, row in confirmed.iterrows():
        name = html.escape(str(row.get(symbol_col, "N/A")))
        movement = str(row.get("MOVEMENT STATUS", "")).upper()
        direction = "UP ↑" if "BIG BUY" in movement else "DOWN ↓"
        cls = "bigmove-up" if "BIG BUY" in movement else "bigmove-down"
        score = row.get("SCORE", "")
        try:
            score_text = f" · Score {float(score):.0f}" if pd.notna(score) and str(score).strip() else ""
        except Exception:
            score_text = ""
        chips.append(f'<span class="bigmove-chip {cls}">{name} · {direction}{html.escape(score_text)}</span>')
    st.markdown("".join(chips) + "</div>", unsafe_allow_html=True)



# ===== ADDITIVE HIGH/LOW BREAKOUT AND AMD SCANNERS =====
def _new_signal_time_ist(candle_time, minutes=5):
    """Signal confirmation time = completed candle end, in IST."""
    ts = pd.Timestamp(candle_time)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return (ts.tz_convert("Asia/Kolkata") + pd.Timedelta(minutes=minutes)).strftime("%d-%b-%Y %H:%M:%S IST")


def _new_breakout_signal(df):
    if df is None or len(df) < 10 or "Time" not in df.columns:
        return None
    d = df.copy().sort_values("Time")
    t = pd.to_datetime(d["Time"], utc=True).dt.tz_convert("Asia/Kolkata")
    d["_date"] = t.dt.date
    dates = sorted(d["_date"].unique())
    if len(dates) < 2:
        return None
    today, previous = dates[-1], dates[-2]
    prev = d[d["_date"] == previous]
    cur = d[d["_date"] == today].copy()
    if cur.empty or prev.empty:
        return None
    # FYERS 5-minute candles: first three candles cover 09:15–09:30 IST.
    first = cur[(pd.to_datetime(cur["Time"], utc=True).dt.tz_convert("Asia/Kolkata").dt.hour == 9) &
                (pd.to_datetime(cur["Time"], utc=True).dt.tz_convert("Asia/Kolkata").dt.minute.isin([15,20,25]))]
    if len(first) != 3:
        return None
    last = cur.iloc[-1]
    before = cur.iloc[-2] if len(cur) > 1 else None
    price = float(last["Close"])
    p_high, p_low = float(prev["High"].max()), float(prev["Low"].min())
    o_high, o_low = float(first["High"].max()), float(first["Low"].min())
    avgvol = float(d["Volume"].iloc[-21:-1].mean()) if len(d) >= 21 else float(d["Volume"].iloc[:-1].mean())
    rvol = float(last["Volume"]) / avgvol if avgvol > 0 else 0.0
    result = {"LTP": round(price,2), "PREVIOUS DAY": str(previous),
              "PREV HIGH": round(p_high,2), "PREV LOW": round(p_low,2),
              "FIRST 15M HIGH": round(o_high,2), "FIRST 15M LOW": round(o_low,2),
              "RVOL": round(rvol,2), "CANDLE TIME": str(last["Time"]),
              "SIGNAL TIME": ""}
    for label, hi, lo in [("PREVIOUS DAY",p_high,p_low),("TODAY 15M",o_high,o_low)]:
        # Only show a new cross on the latest completed candle, not stale breaks.
        prior_close = float(before["Close"]) if before is not None else price
        eligible = label == "PREVIOUS DAY" or len(cur) > 3
        up = eligible and prior_close <= hi and price > hi
        down = eligible and prior_close >= lo and price < lo
        if up or down:
            direction = "BUY" if up else "SELL"
            result[label+" SIGNAL"] = direction if rvol >= 1.2 else "WAIT VOLUME"
            if rvol >= 1.2:
                result[label+" SIGNAL TIME"] = _new_signal_time_ist(last["Time"])
                result["SIGNAL TIME"] = _new_signal_time_ist(last["Time"])
            else:
                result[label+" SIGNAL TIME"] = ""
            result[label+" LEVEL"] = round(hi if up else lo,2)
        else:
            result[label+" SIGNAL"] = "WAIT"
            result[label+" SIGNAL TIME"] = ""
            result[label+" LEVEL"] = None
    return result


def _new_scan_tab(fyers, all_symbols, fo_symbols, kind):
    title = "📈 PREVIOUS DAY + TODAY 15M HIGH/LOW BREAKOUT" if kind == "breakout" else "🧠 AMD — ACCUMULATION / MANIPULATION / DISTRIBUTION"
    st.subheader(title)
    universe_type = st.radio("Universe", ["F&O Stocks", "NSE Stocks"], horizontal=True, key=kind+"_universe")
    universe = fo_symbols if universe_type == "F&O Stocks" else all_symbols
    if not universe:
        st.warning("No symbols available")
        return
    limit = st.number_input("Stocks to scan (0 = ALL)", 0, len(universe), 0, 10, key=kind+"_limit")
    symbols = universe if limit == 0 else universe[:limit]
    if st.button("🔍 RUN " + kind.upper() + " SCAN", key=kind+"_run", type="primary"):
        rows, errors = [], []
        progress = st.progress(0)
        for idx, sym in enumerate(symbols):
            try:
                d = _fetch_timeframe_data(fyers, sym, "5", lookback_days=8)
                if d is None:
                    errors.append(sym+": data unavailable")
                elif kind == "breakout":
                    row = _new_breakout_signal(d)
                    if row is not None:
                        rows.append({"SYMBOL":sym, **row})
                else:
                    phase = _detect_amd_phase(d)
                    rows.append({"SYMBOL":sym,"LTP":round(float(d["Close"].iloc[-1]),2),
                                 "CANDLE TIME":str(d["Time"].iloc[-1]),
                                 "SIGNAL TIME": _new_signal_time_ist(d["Time"].iloc[-1]) if phase.get("signal") not in ("WAIT", "WAIT CONFIRMATION", None) else "",
                                 "AMD PHASE":phase.get("phase"),"DIRECTION":phase.get("direction"),
                                 "SIGNAL":phase.get("signal"),"REASON":phase.get("reason")})
            except Exception as exc:
                errors.append(sym+": "+str(exc)[:100])
            progress.progress((idx+1)/len(symbols))
        st.session_state[kind+"_results"] = pd.DataFrame(rows)
        st.session_state[kind+"_errors"] = errors
    result = st.session_state.get(kind+"_results")
    if isinstance(result,pd.DataFrame):
        st.caption(f"Results: {len(result)} | Failed/skipped: {len(st.session_state.get(kind+'_errors',[]))}")
        if not result.empty:
            if kind == "breakout":
                view_type = st.selectbox("Breakout view",["ALL","PREVIOUS DAY","TODAY 15M"],key="breakout_view")
                if view_type != "ALL":
                    result = result[result[view_type+" SIGNAL"].ne("WAIT")]
            else:
                phase_filter = st.selectbox("AMD Phase",["ALL","ACCUMULATION","MANIPULATION","DISTRIBUTION","CONFIRMATION"],key="amd_phase_filter")
                if phase_filter != "ALL":
                    result = result[result["AMD PHASE"].eq(phase_filter)]
            st.dataframe(result,use_container_width=True,hide_index=True,height=550)
            st.download_button("📥 DOWNLOAD CSV",result.to_csv(index=False).encode("utf-8-sig"),
                               file_name=kind+"_scanner.csv",mime="text/csv",key=kind+"_csv")
        else:
            st.info("No matching results; check data availability and scan conditions.")
    st.caption("Signals use completed 5-minute candles. WAIT is not a confirmed trade signal. AMD is heuristic, not proof of institutional activity.")


# ========================================================================
# ADD-ON: HH/HL / LH/LL + CANDLESTICK PATTERN SEARCH
# Uses existing FYERS history fetcher and scans only completed candles.
# ========================================================================
def _pattern_and_structure_summary(df):
    """Return confirmed swing structure plus latest complete candle patterns."""
    import math
    out = {"STRUCTURE": "NEUTRAL", "PATTERN": "NONE", "PATTERN BIAS": "NEUTRAL",
           "LAST CLOSE": None, "BREAKOUT LEVEL": None, "BREAKDOWN LEVEL": None,
           "DIST HIGH %": None, "DIST LOW %": None, "RVOL": None,
           "PRE-MOVE STATUS": "WAIT", "REASON": "Insufficient data"}
    if df is None or len(df) < 25:
        return out
    d = df.reset_index(drop=True)
    a, b = d.iloc[-2], d.iloc[-1]
    o, h, l, c = [float(b[k]) for k in ("Open", "High", "Low", "Close")]
    po, pc = float(a["Open"]), float(a["Close"])
    if not all(math.isfinite(x) for x in (o,h,l,c,po,pc)) or h < l or min(o,c) <= 0:
        return out
    rng = max(h-l, 1e-10)
    body = abs(c-o)
    upper = h-max(o,c)
    lower = min(o,c)-l
    bull_engulf = pc < po and c > o and o <= pc and c >= po and c > po
    bear_engulf = pc > po and c < o and o >= pc and c <= po and c < po
    if bull_engulf:
        pattern, bias = "BULLISH ENGULFING", "UP WATCH"
    elif bear_engulf:
        pattern, bias = "BEARISH ENGULFING", "DOWN WATCH"
    elif body/rng <= .10:
        pattern, bias = "DOJI", "NEUTRAL"
    elif lower >= 2 * max(body, .001*rng) and upper <= .30*rng and body/rng < .40:
        pattern, bias = "HAMMER-SHAPE", "POTENTIAL UP REVERSAL"
    elif upper >= 2 * max(body, .001*rng) and lower <= .30*rng and body/rng < .40:
        pattern, bias = "SHOOTING-STAR-SHAPE", "POTENTIAL DOWN REVERSAL"
    else:
        pattern, bias = "NONE", "NEUTRAL"
    ph, pl = _confirmed_pivots(d, left=2, right=2)
    structure = "NEUTRAL"
    if len(ph) >= 2 and len(pl) >= 2:
        if ph[-1][1] > ph[-2][1] and pl[-1][1] > pl[-2][1]:
            structure = "HH/HL"
        elif ph[-1][1] < ph[-2][1] and pl[-1][1] < pl[-2][1]:
            structure = "LH/LL"
        elif ph[-1][1] > ph[-2][1]:
            structure = "HH ONLY"
        elif pl[-1][1] > pl[-2][1]:
            structure = "HL ONLY"
        elif ph[-1][1] < ph[-2][1]:
            structure = "LH ONLY"
        elif pl[-1][1] < pl[-2][1]:
            structure = "LL ONLY"
    # Previous closed-candle range: don't include current candle in levels
    previous = d.iloc[-11:-1]
    top, bottom = float(previous["High"].max()), float(previous["Low"].min())
    dv = pd.to_numeric(d["Volume"], errors="coerce")
    prior_mean = float(dv.iloc[-21:-1].mean())
    rvol = float(dv.iloc[-1])/prior_mean if prior_mean > 0 else 0.0
    dist_hi = 100*(top-c)/top if top > 0 else None
    dist_lo = 100*(c-bottom)/bottom if bottom > 0 else None
    premove = "WAIT"
    if structure == "HH/HL" and dist_hi is not None and 0 <= dist_hi <= .5 and rvol >= 1.15:
        premove = "UP PRE-MOVE WATCH"
    elif structure == "LH/LL" and dist_lo is not None and 0 <= dist_lo <= .5 and rvol >= 1.15:
        premove = "DOWN PRE-MOVE WATCH"
    elif dist_hi is not None and dist_hi < 0 and structure == "HH/HL":
        premove = "UP BREAKOUT ALREADY STARTED"
    elif dist_lo is not None and dist_lo < 0 and structure == "LH/LL":
        premove = "DOWN BREAKOUT ALREADY STARTED"
    out.update({"STRUCTURE": structure, "PATTERN": pattern, "PATTERN BIAS": bias,
                "LAST CLOSE": round(c,2), "BREAKOUT LEVEL": round(top,2),
                "BREAKDOWN LEVEL": round(bottom,2), "DIST HIGH %": round(dist_hi,2) if dist_hi is not None else None,
                "DIST LOW %": round(dist_lo,2) if dist_lo is not None else None,
                "RVOL": round(rvol,2), "PRE-MOVE STATUS": premove,
                "REASON": "Price structure + pattern are screening evidence, not confirmed predictions"})
    return out


def _run_pattern_scan(fyers, symbols, resolution, scan_limit):
    rows, errors = [], []
    chosen = symbols if scan_limit == 0 else symbols[:scan_limit]
    progress = st.progress(0)
    for i, sym in enumerate(chosen):
        try:
            df = _fetch_timeframe_data(fyers, sym, resolution, lookback_days=12 if resolution in ("5","15") else 55)
            if df is None or len(df) < 25:
                errors.append({"SYMBOL":sym,"ISSUE":"Insufficient closed OHLCV candles"})
            else:
                result = _pattern_and_structure_summary(df)
                rows.append({"SYMBOL":sym,"TIMEFRAME":resolution+"m" if resolution != "D" else "1D",
                             "CANDLE TIME":str(df["Time"].iloc[-1]),**result})
        except Exception as exc:
            errors.append({"SYMBOL":sym,"ISSUE":str(exc)[:120]})
        progress.progress((i+1)/max(len(chosen),1))
    progress.empty()
    return pd.DataFrame(rows), pd.DataFrame(errors)


def _show_pattern_scan_tab(fyers, all_symbols, fo_symbols, mode):
    st.markdown("### " + ("📈 HH/HL • LH/LL Structure Search" if mode == "structure" else "🕯️ Candlestick Pattern Search"))
    st.caption("Completed candle data only. Pattern recognition is indicative; not an assured BUY/SELL or predictive confidence.")
    u, t, n = st.columns(3)
    with u:
        universe = st.selectbox("Universe",["F&O Stocks","NSE Stocks"],key=f"{mode}_universe")
    stocks = fo_symbols if universe == "F&O Stocks" else all_symbols
    with t:
        tf = st.selectbox("Timeframe",["5","15","60","D"],format_func=lambda v: "1 Day" if v=="D" else f"{v} min",key=f"{mode}_tf")
    with n:
        limit = st.number_input("Stock count (0 = ALL)",min_value=0,max_value=max(0,len(stocks)),value=min(50,len(stocks)),key=f"{mode}_limit")
    if st.button("🔎 RUN " + ("HH/HL SEARCH" if mode=="structure" else "CANDLESTICK SEARCH"),key=f"{mode}_run",type="primary"):
        if not stocks:
            st.warning("No stocks available for this universe")
        else:
            with st.spinner("Fetching completed candles from FYERS..."):
                result, errors = _run_pattern_scan(fyers,stocks,tf,int(limit))
                st.session_state[f"{mode}_results"] = result
                st.session_state[f"{mode}_errors"] = errors
    result = st.session_state.get(f"{mode}_results")
    if result is not None:
        if result.empty:
            st.warning("No candle data returned. Check market history permissions and selected timeframe.")
        else:
            options = (["ALL","HH/HL","LH/LL","HH ONLY","HL ONLY","LH ONLY","LL ONLY","NEUTRAL"]
                       if mode == "structure" else
                       ["ALL","BULLISH ENGULFING","BEARISH ENGULFING","DOJI","HAMMER-SHAPE","SHOOTING-STAR-SHAPE","NONE"])
            label = "Structure" if mode=="structure" else "Candlestick pattern"
            selected = st.selectbox(label,options,key=f"{mode}_filter")
            field = "STRUCTURE" if mode=="structure" else "PATTERN"
            view = result if selected == "ALL" else result[result[field].eq(selected)]
            pre_only = st.checkbox("Only PRE-MOVE WATCH candidates",key=f"{mode}_preonly")
            if pre_only:
                view = view[view["PRE-MOVE STATUS"].str.contains("PRE-MOVE WATCH",na=False)]
            st.write(f"Matches: {len(view)} / Scanned with candles: {len(result)}")
            st.dataframe(view,use_container_width=True,hide_index=True,height=520)
            st.download_button("📥 Download results CSV",view.to_csv(index=False).encode("utf-8-sig"),
                               file_name=f"{mode}_search.csv",mime="text/csv",key=f"{mode}_download")
    errors = st.session_state.get(f"{mode}_errors")
    if isinstance(errors,pd.DataFrame) and not errors.empty:
        with st.expander(f"Skipped/errors: {len(errors)}"):
            st.dataframe(errors,use_container_width=True,hide_index=True)


def show_scanner(fyers) -> None:
    """Streamlit main app - NSE AI PRO V17 with MOMENTUM MOVERS"""
    
    try:
        st.set_page_config(page_title="NSE AI PRO V17", layout="wide")
    except:
        pass
    
    st.title("🚀 NSE AI PRO V17 — Professional Intraday + Swing Scanner")
    st.caption(f"🕒 Current Time (IST): {_now_ist().strftime('%d-%b-%Y %H:%M:%S')} IST | Multi-Timeframe + Momentum Engine")
    _show_big_movement_alert_box()
    
    try:
        all_symbols = load_nse_equity_symbols()
        fo_symbols = load_fo_stocks()
        st.session_state["fo_symbols"] = fo_symbols
    except Exception as e:
        st.error(f"❌ Error loading symbols: {e}")
        logger.error(f"Symbol loading error: {e}")
        return
    
    if not all_symbols:
        st.error("❌ No symbols loaded — check FYERS API access.")
        return
    
    st.caption(f"📊 NSE Equities: {len(all_symbols)} | 📈 F&O Stocks: {len(fo_symbols)}")
    
    tabs = st.tabs([
        "🇮🇳 NSE STOCKS",
        "📊 F&O STOCKS",
        "⚡ MOMENTUM MOVERS",
        "🚦 BEFORE MOVE",
        "📖 LIVE ORDER BOOK",
        "⚡ LIVE INTRADAY",
        "🔥 STRONG SIGNALS",
        "📈 SWING (GOLDEN/DEATH CROSS)",
        "🧠 ADDITIONAL ANALYSIS",
        "📊 MARKET DASHBOARD",
        "⚙️ SETTINGS",
        "📌 PIN RULES",
        "🎯 F&O OPTION CHECK",
        "🔄 REVERSAL",
        "🤖 AI CHART ANALYSIS",
        "🎯 PRE-MOVE CONFIRM",
        "📈 HIGH/LOW BREAKOUT",
        "🧠 AMD SCANNER",
        "📊 ADDITIONAL INDICATORS",
        "⏱️ 20M RANGE BIG MOVE",
        "🎯 PRE-BIG STOCK FINDER",
        "📈 HH/HL STRUCTURE SEARCH",
        "🕯️ CANDLESTICK SEARCH",
        "⚡ EARLY BIG MOVE WATCH"
    ])
    
    # ════════════════════════════════════════════════════════════════════════════════
    # TAB 0: NSE STOCKS
    # ════════════════════════════════════════════════════════════════════════════════
    with tabs[0]:
        st.markdown("### NSE Equity Stocks Scanner\n✅ Strict validation - only high-quality signals")
        
        col1, col2 = st.columns([3, 1])
        with col1:
            nse_limit = st.number_input(
                "Scan limit (0=ALL NSE)",
                min_value=0, max_value=len(all_symbols),
                value=0, step=50, key="nse_limit"
            )
        with col2:
            st.metric("Available", len(all_symbols))
        
        nse_universe = all_symbols if nse_limit == 0 else all_symbols[:nse_limit]
        st.info(f"🇮🇳 NSE total symbols available: {len(all_symbols)} | Selected for scan: {len(nse_universe)}")
        
        if st.button(f"🔍 SCAN NSE ({len(nse_universe)} stocks)", key="nse_run", type="primary"):
            with st.spinner("Analyzing NSE stocks…"):
                nse_results, nse_errors, nse_stats = run_nse_scan(fyers, nse_universe)
                st.session_state["nse_df"] = pd.DataFrame(nse_results) if nse_results else pd.DataFrame()
                st.session_state["nse_errors"] = nse_errors
                st.session_state["nse_stats"] = nse_stats
        
        if "nse_stats" in st.session_state:
            _display_scan_summary(st.session_state["nse_stats"])
        
        nse_df = st.session_state.get("nse_df")
        if nse_df is not None and not nse_df.empty:
            try:
                st.info(f"📊 Loaded: {len(nse_df)} signals")
                
                col_f1, col_f2, col_f3 = st.columns(3)
                
                with col_f1:
                    nse_min_conf = st.slider("Min Confidence %", 0, 100, 70, step=5, key="nse_conf_filter")
                with col_f2:
                    nse_signal = st.selectbox("Signal", ["ALL", "BUY", "SELL", "STRONG ONLY"], key="nse_signal_filter")
                with col_f3:
                    nse_sort = st.selectbox("Sort By", ["AI CONFIDENCE %", "LTP", "RVOL"], key="nse_sort_filter")
                
                nse_filtered = nse_df.copy()
                try:
                    confidence_col = pd.to_numeric(nse_filtered["AI CONFIDENCE %"], errors='coerce')
                    nse_filtered = nse_filtered[confidence_col >= nse_min_conf]
                except:
                    pass
                
                if nse_signal != "ALL":
                    try:
                        normalized = nse_filtered["AI SIGNAL"].apply(normalize_signal)
                        if nse_signal == "BUY":
                            nse_filtered = nse_filtered[normalized == "BUY"]
                        elif nse_signal == "SELL":
                            nse_filtered = nse_filtered[normalized == "SELL"]
                        elif nse_signal == "STRONG ONLY":
                            nse_filtered = nse_filtered[nse_filtered["AI SIGNAL"].astype(str).str.contains("STRONG", na=False)]
                    except:
                        pass
                
                try:
                    if nse_sort == "LTP":
                        nse_filtered = nse_filtered.sort_values("LTP", ascending=False)
                    elif nse_sort == "RVOL":
                        nse_filtered = nse_filtered.sort_values("RVOL", ascending=False)
                    else:
                        nse_filtered = nse_filtered.sort_values("AI CONFIDENCE %", ascending=False)
                except:
                    pass
                
                nse_filtered = _add_reversal_columns(nse_filtered)
                st.dataframe(nse_filtered, use_container_width=True, height=500, hide_index=True)
                
                st.markdown("### 📥 Download")
                col_d1, col_d2, col_d3 = st.columns(3)
                
                with col_d1:
                    try:
                        excel_data = _format_excel_output(nse_filtered, "NSE")
                        st.download_button("📊 Excel", excel_data, f"NSE_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx",
                                         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="nse_xls")
                    except Exception as e:
                        st.error(f"❌ Excel: {str(e)[:50]}")
                
                with col_d2:
                    try:
                        csv_data = to_csv_bytes(nse_filtered)
                        st.download_button("📄 CSV", csv_data, f"NSE_{_now_ist().strftime('%Y%m%d_%H%M')}.csv",
                                         "text/csv", key="nse_csv")
                    except Exception as e:
                        st.error(f"❌ CSV: {str(e)[:50]}")
                
                with col_d3:
                    try:
                        json_data = to_json_bytes(nse_filtered)
                        st.download_button("📋 JSON", json_data, f"NSE_{_now_ist().strftime('%Y%m%d_%H%M')}.json",
                                         "application/json", key="nse_json")
                    except Exception as e:
                        st.error(f"❌ JSON: {str(e)[:50]}")
            
            except Exception as e:
                st.error(f"❌ NSE Tab Error: {str(e)[:100]}")
        else:
            st.info("👈 Click 'SCAN NSE' to start")
    
    # ════════════════════════════════════════════════════════════════════════════════
    # TAB 1: F&O STOCKS
    # ════════════════════════════════════════════════════════════════════════════════
    with tabs[1]:
        st.markdown("### F&O Stocks Scanner\n✅ Strict validation + options analysis")
        
        col1, col2 = st.columns([3, 1])
        with col1:
            fo_limit = st.number_input("Scan limit (0=ALL F&O)", min_value=0, max_value=len(fo_symbols),
                                      value=0, step=25, key="fo_limit")
        with col2:
            st.metric("Available", len(fo_symbols))
        
        fo_universe = fo_symbols if fo_limit == 0 else fo_symbols[:fo_limit]
        
        # F&O scan button: keep the scan result in session_state so it
        # survives Streamlit reruns and can be downloaded immediately.
        if st.button(
            f"🔍 SCAN F&O ({len(fo_universe)} stocks)",
            key="fo_run",
            type="primary",
            use_container_width=True,
        ):
            with st.spinner(f"Analyzing F&O stocks ({len(fo_universe)} symbols)…"):
                try:
                    fo_results, fo_errors, fo_stats = run_fo_scan(fyers, fo_universe)
                    st.session_state["fo_df"] = (
                        pd.DataFrame(fo_results) if fo_results else pd.DataFrame()
                    )
                    st.session_state["fo_errors"] = fo_errors or []
                    st.session_state["fo_stats"] = fo_stats
                except Exception as e:
                    st.session_state["fo_df"] = pd.DataFrame()
                    st.session_state["fo_errors"] = [str(e)]
                    st.error(f"❌ F&O scan failed: {type(e).__name__}: {str(e)[:180]}")
        
        if "fo_stats" in st.session_state:
            _display_scan_summary(st.session_state["fo_stats"])
        
        fo_df = st.session_state.get("fo_df")
        if fo_df is not None and not fo_df.empty:
            try:
                st.info(f"📊 Loaded: {len(fo_df)} signals")
                
                col_f1, col_f2, col_f3, col_f4 = st.columns(4)
                with col_f1:
                    fo_min_conf = st.slider("Min Confidence %", 0, 100, 70, step=5, key="fo_conf_filter")
                with col_f2:
                    fo_signal = st.selectbox("Signal", ["ALL", "BUY", "SELL", "STRONG ONLY"], key="fo_signal_filter")
                with col_f3:
                    fo_opt = st.selectbox("Options", ["ALL", "BULLISH", "BEARISH"], key="fo_opt_filter")
                with col_f4:
                    fo_sort = st.selectbox("Sort By", ["AI CONFIDENCE %", "LTP", "PCR"], key="fo_sort_filter")
                
                fo_filtered = fo_df.copy()
                try:
                    confidence_col = pd.to_numeric(fo_filtered["AI CONFIDENCE %"], errors='coerce')
                    fo_filtered = fo_filtered[confidence_col >= fo_min_conf]
                except:
                    pass
                
                if fo_signal != "ALL":
                    try:
                        normalized = fo_filtered["AI SIGNAL"].apply(normalize_signal)
                        if fo_signal == "BUY":
                            fo_filtered = fo_filtered[normalized == "BUY"]
                        elif fo_signal == "SELL":
                            fo_filtered = fo_filtered[normalized == "SELL"]
                        elif fo_signal == "STRONG ONLY":
                            fo_filtered = fo_filtered[fo_filtered["AI SIGNAL"].astype(str).str.contains("STRONG", na=False)]
                    except:
                        pass
                
                if fo_opt != "ALL":
                    try:
                        fo_filtered = fo_filtered[fo_filtered["OPTIONS BIAS"].astype(str).str.contains(fo_opt, na=False)]
                    except:
                        pass
                
                try:
                    if fo_sort == "PCR":
                        fo_filtered = fo_filtered.sort_values("PCR", ascending=False)
                    elif fo_sort == "LTP":
                        fo_filtered = fo_filtered.sort_values("LTP", ascending=False)
                    else:
                        fo_filtered = fo_filtered.sort_values("AI CONFIDENCE %", ascending=False)
                except:
                    pass
                
                fo_filtered = _add_reversal_columns(fo_filtered)
                st.dataframe(fo_filtered, use_container_width=True, height=500, hide_index=True)
                
                st.markdown("### 📥 Download")
                col_d1, col_d2, col_d3 = st.columns(3)
                
                with col_d1:
                    _excel_download_button(
                        fo_filtered,
                        "FO",
                        "fo_xls_filtered",
                        label="📊 Excel",
                    )
                
                with col_d2:
                    try:
                        csv_data = to_csv_bytes(fo_filtered)
                        st.download_button("📄 CSV", csv_data, f"FO_{_now_ist().strftime('%Y%m%d_%H%M')}.csv",
                                         "text/csv", key="fo_csv")
                    except:
                        pass
                
                with col_d3:
                    try:
                        json_data = to_json_bytes(fo_filtered)
                        st.download_button("📋 JSON", json_data, f"FO_{_now_ist().strftime('%Y%m%d_%H%M')}.json",
                                         "application/json", key="fo_json")
                    except:
                        pass
            
            except Exception as e:
                st.error(f"❌ F&O Tab Error: {str(e)[:100]}")
        else:
            st.info("👈 Click 'SCAN F&O' to start")
    
    # ════════════════════════════════════════════════════════════════════════════════
    # TAB 2: MOMENTUM MOVERS — INTRADAY PRE-MOVE + BIG MOVE
    # ════════════════════════════════════════════════════════════════════════════════
    with tabs[2]:
        st.markdown("### ⚡ INTRADAY MOVEMENT — PRE-MOVE + BIG BUY / BIG SELL")
        st.caption("Checks recent completed 5M candles. Early warning before movement + confirmed movement after breakout/acceleration.")
        col_m1,col_m2,col_m3=st.columns([2,2,1])
        with col_m1:
            momentum_type=st.radio("Select Universe",["NSE Stocks","F&O Stocks"],horizontal=True,key="momentum_type")
            momentum_universe=all_symbols if momentum_type=="NSE Stocks" else fo_symbols
        with col_m2:
            momentum_limit=st.number_input("Scan limit",min_value=50,max_value=len(momentum_universe),value=min(500,len(momentum_universe)),step=50,key="momentum_limit")
        with col_m3: st.metric("Available",len(momentum_universe))
        momentum_symbols=momentum_universe[:momentum_limit]
        if st.button(f"⚡ SCAN INTRADAY MOVEMENT ({len(momentum_symbols)} stocks)",key="momentum_run",type="primary"):
            with st.spinner("Scanning recent 5M candles for PRE-MOVE and BIG BUY/SELL…"):
                is_fo=momentum_type=="F&O Stocks"; rr,ee,ss=run_momentum_scan(fyers,momentum_symbols,is_fo=is_fo)
                st.session_state["momentum_df"]=pd.DataFrame(rr) if rr else pd.DataFrame(); st.session_state["momentum_errors"]=ee; st.session_state["momentum_stats"]=ss; st.session_state["momentum_scanned_at"]=_generated_timestamp()
        if "momentum_stats" in st.session_state:
            _display_scan_summary(st.session_state["momentum_stats"]); st.caption(f"Last scan: {st.session_state.get('momentum_scanned_at','N/A')}")
        mdf=st.session_state.get("momentum_df")
        if mdf is not None and not mdf.empty:
            for col in ["SCORE","PRE-MOVE SCORE","BEFORE MOVE SCORE","RVOL","SIGNAL AGE (MIN)","PRE-MOVE VOLUME SCORE","VOLUME BUILDUP X"]: mdf[col]=pd.to_numeric(mdf[col],errors="coerce") if col in mdf.columns else 0
            status_order={"🔥 BIG BUY":0,"🔥 BIG SELL":0,"🟢 BIG VOLUME BUY WATCH":1,"🔴 BIG VOLUME SELL WATCH":1,"🟢 BUY BEFORE MOVE":2,"🔴 SELL BEFORE MOVE":2,"🟢 BIG BUY WATCH":3,"🔴 BIG SELL WATCH":3,"🟡 VOLUME BUILDUP — WAIT":4,"🟡 PRE-BIG BUY":5,"🟡 PRE-BIG SELL":5,"🟢 BUY":6,"🔴 SELL":6,"⚪ WAIT":9}
            mdf["_order"]=mdf["MOVEMENT STATUS"].map(status_order).fillna(8)
            mdf=mdf.sort_values(["_order","PRE-MOVE VOLUME SCORE","SCORE","PRE-MOVE SCORE","RVOL"],ascending=[True,False,False,False,False]).drop(columns=["_order"])
            # Keep stock name as the first visible column in the movement table.
            _momentum_symbol_col = next((c for c in ["SYMBOL", "Symbol", "symbol"] if c in mdf.columns), None)
            if _momentum_symbol_col is not None and _momentum_symbol_col != "STOCK NAME":
                mdf=mdf.rename(columns={_momentum_symbol_col:"STOCK NAME"})
            if "STOCK NAME" not in mdf.columns:
                mdf.insert(0,"STOCK NAME","N/A")
            _priority_cols=["STOCK NAME", "MOVEMENT STATUS", "BREAKOUT CHECK", "FALSE BREAKOUT RISK", "BREAKOUT QUALITY SCORE", "RALLY OUTLOOK", "EST. CONTINUATION", "BREAKOUT CHECK REASON"]
            _momentum_cols=[c for c in _priority_cols if c in mdf.columns]+[c for c in mdf.columns if c not in _priority_cols]
            mdf=mdf[_momentum_cols]
            mdf=_add_reversal_columns(mdf)
            st.markdown("### 🚦 MOVEMENT STATUS")
            st.dataframe(mdf,use_container_width=True,height=560,hide_index=True)
            st.download_button("📊 Excel",_format_excel_output(mdf,"INTRADAY_MOVEMENT"),f"INTRADAY_MOVEMENT_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx","application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",key="momentum_xls")
        else: st.info("Click SCAN INTRADAY MOVEMENT to start.")

    # ════════════════════════════════════════════════════════════════════════════════
    # TAB 3: BEFORE MOVE — EARLY WARNING SIGNAL
    # ════════════════════════════════════════════════════════════════════════════════
    with tabs[3]:
        st.markdown("### 🚦 BEFORE MOVE — EARLY WARNING SIGNAL")
        st.caption("Separate early-warning tab. Old Momentum / NSE / F&O signals remain unchanged.")
        bdf = st.session_state.get("momentum_df")
        if bdf is not None and not bdf.empty:
            view = bdf.copy()
            # Always expose the stock name first; FYERS/session data may use
            # SYMBOL, Symbol or symbol depending on the scan path.
            _symbol_col = next((c for c in ["SYMBOL", "Symbol", "symbol"] if c in view.columns), None)
            if _symbol_col is not None:
                view = view.rename(columns={_symbol_col: "STOCK NAME"})
            elif "STOCK NAME" not in view.columns:
                view.insert(0, "STOCK NAME", "N/A")
            view = _add_reversal_columns(view)
            st.markdown("#### 🔊 BIG VOLUME BEFORE-MOVE FILTER")
            vc1, vc2 = st.columns(2)
            with vc1:
                min_volume_buildup = st.slider("Minimum volume buildup (x)", 1.0, 3.0, 1.5, 0.1, key="before_move_volume_buildup")
            with vc2:
                before_volume_filter = st.selectbox("Volume pre-move status", ["ALL", "BIG VOLUME BUY WATCH", "BIG VOLUME SELL WATCH", "VOLUME BUILDUP — WAIT"], key="before_move_volume_filter")
            # Preserve the candle-based signal timestamp in the BEFORE MOVE report.
            # Older scan paths may expose it as "Signal Time" rather than "SIGNAL TIME".
            if "SIGNAL TIME" not in view.columns and "Signal Time" in view.columns:
                view = view.rename(columns={"Signal Time": "SIGNAL TIME"})
            _before_cols = ["STOCK NAME", "LTP", "SIGNAL TIME", "Signal Time", "PRE-MOVE VOLUME STATUS", "PRE-MOVE VOLUME SCORE", "VOLUME BUILDUP X", "RANGE COMPRESSION %", "PRE-MOVE VOLUME DIRECTION", "DISTANCE TO RANGE HIGH %", "DISTANCE TO RANGE LOW %", "PRE-MOVE VOLUME REASON", "BEFORE MOVE SIGNAL", "BEFORE MOVE SCORE", "PRE-MOVE SCORE", "PRE-MOVE STATUS", "PRE BUY/SELL SCORE", "PRE SCORE GAP", "BREAKOUT LEVEL", "BREAKDOWN LEVEL", "PRE-MOVE RVOL", "RVOL", "VOLUME SPIKE", "PRE-MOVE REASON", "REVERSAL SIGNAL", "REVERSAL SCORE", "REVERSAL LEVEL", "REVERSAL ZONE", "REVERSAL REASON", "SIGNAL TYPE", "COMBINED BIAS"]
            cols = [c for c in _before_cols if c in view.columns]
            view = view[cols].copy() if cols else view.copy()
            if "VOLUME BUILDUP X" in view.columns:
                view["VOLUME BUILDUP X"] = pd.to_numeric(view["VOLUME BUILDUP X"], errors="coerce").fillna(0)
                view = view[view["VOLUME BUILDUP X"] >= min_volume_buildup]
            if before_volume_filter != "ALL" and "PRE-MOVE VOLUME STATUS" in view.columns:
                if before_volume_filter == "BIG VOLUME BUY WATCH":
                    view = view[view["PRE-MOVE VOLUME STATUS"] == "🟢 BIG VOLUME BUY WATCH"]
                elif before_volume_filter == "BIG VOLUME SELL WATCH":
                    view = view[view["PRE-MOVE VOLUME STATUS"] == "🔴 BIG VOLUME SELL WATCH"]
                else:
                    view = view[view["PRE-MOVE VOLUME STATUS"] == "🟡 VOLUME BUILDUP — WAIT"]
            st.caption("Volume buildup is recent 3-bar average volume ÷ preceding 17-bar average volume. These are early-watch candidates, not guaranteed moves.")
            if "BEFORE MOVE SIGNAL" in view.columns:
                order = {"🟢 BUY BEFORE MOVE": 0, "🔴 SELL BEFORE MOVE": 1, "🟡 WAIT": 2}
                view["_sort"] = view["BEFORE MOVE SIGNAL"].map(order).fillna(9)
                score_col = "BEFORE MOVE SCORE" if "BEFORE MOVE SCORE" in view.columns else "PRE-MOVE SCORE"
                view = view.sort_values(["_sort", score_col], ascending=[True, False]).drop(columns=["_sort"])
            if view.empty:
                st.info("No stocks match the selected volume buildup/status filters. Lower the buildup threshold or choose ALL.")
            else:
                st.dataframe(view, use_container_width=True, height=560, hide_index=True)
                st.download_button("📊 Download BEFORE MOVE", _format_excel_output(view, "BEFORE_MOVE"), f"BEFORE_MOVE_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="before_move_xls")
        else:
            st.info("Run SCAN INTRADAY MOVEMENT first. The separate BEFORE MOVE tab will then show the early-warning signals.")

    # ════════════════════════════════════════════════════════════════════════════════
    # TAB 4: LIVE EXCHANGE ORDER BOOK
    # ════════════════════════════════════════════════════════════════════════════════
    with tabs[4]:
        _show_live_order_flow_tab(fyers, all_symbols)

    # ════════════════════════════════════════════════════════════════════════════════
    # TAB 4: LIVE INTRADAY
    # ════════════════════════════════════════════════════════════════════════════════
    with tabs[5]:
        st.markdown("### ⚡ Live Intraday Scanner\nReal-time multi-timeframe analysis (5M, 15M, 1H)")
        
        col1, col2 = st.columns(2)
        with col1:
            intraday_type = st.radio("Select Universe", ["NSE Stocks", "F&O Stocks"], horizontal=True, key="intraday_type")
            intraday_universe = all_symbols if intraday_type == "NSE Stocks" else fo_symbols
        
        with col2:
            intraday_limit = st.number_input("Scan limit", min_value=10, max_value=len(intraday_universe),
                                            value=min(100, len(intraday_universe)), step=10, key="intraday_limit")
        
        intraday_symbols = intraday_universe[:intraday_limit]
        
        if st.button(f"⚡ SCAN LIVE INTRADAY ({len(intraday_symbols)} stocks)", key="intraday_run"):
            with st.spinner("Fetching live intraday data…"):
                if intraday_type == "NSE Stocks":
                    intraday_results, _, _ = run_nse_scan(fyers, intraday_symbols)
                else:
                    intraday_results, _, _ = run_fo_scan(fyers, intraday_symbols)
                
                if intraday_results:
                    intraday_df = pd.DataFrame(intraday_results)
                    st.session_state["intraday_df"] = intraday_df
        
        intraday_df = st.session_state.get("intraday_df")
        if intraday_df is not None and not intraday_df.empty:
            st.success(f"✅ Live data: {len(intraday_df)} stocks")
            
            col_if1, col_if2, col_if3 = st.columns(3)
            with col_if1:
                intraday_min_rvol = st.slider("Min RVOL", 0.5, 3.0, 1.2, 0.1, key="intraday_rvol")
            with col_if2:
                intraday_signal_filter = st.selectbox("Signal", ["ALL", "BUY", "SELL"], key="intraday_sig_filter")
            with col_if3:
                intraday_show_cols = st.multiselect("Show Columns", intraday_df.columns, 
                                                   default=["Symbol", "LTP", "AI SIGNAL", "AI CONFIDENCE %", "RVOL", "🟢 BUY PRESSURE %", "🔴 SELL PRESSURE %"],
                                                   key="intraday_cols")
            
            intraday_filtered = intraday_df.copy()
            try:
                intraday_filtered = intraday_filtered[pd.to_numeric(intraday_filtered["RVOL"], errors='coerce') >= intraday_min_rvol]
            except:
                pass
            
            if intraday_signal_filter != "ALL":
                try:
                    normalized = intraday_filtered["AI SIGNAL"].apply(normalize_signal)
                    if intraday_signal_filter == "BUY":
                        intraday_filtered = intraday_filtered[normalized == "BUY"]
                    elif intraday_signal_filter == "SELL":
                        intraday_filtered = intraday_filtered[normalized == "SELL"]
                except:
                    pass
            
            if intraday_show_cols:
                st.dataframe(intraday_filtered[intraday_show_cols], use_container_width=True, height=400)
            else:
                st.dataframe(intraday_filtered, use_container_width=True, height=400)
        else:
            st.info("👈 Click 'SCAN LIVE INTRADAY' to fetch data")
    
    # ════════════════════════════════════════════════════════════════════════════════
    # TAB 5: STRONG SIGNALS
    # ════════════════════════════════════════════════════════════════════════════════
    with tabs[6]:
        st.markdown("### 🔥 Strong Signals Only\nHigh-confidence setups (≥70%)")
        strong_source = st.radio("Source", ["NSE Stocks", "F&O Stocks"], horizontal=True, key="strong_source")

        strong_universe_all = all_symbols if strong_source == "NSE Stocks" else fo_symbols
        strong_default = min(500 if strong_source == "NSE Stocks" else 200, len(strong_universe_all))
        strong_limit = st.number_input(
            "Strong Signals scan limit (0 = all)",
            min_value=0, max_value=len(strong_universe_all),
            value=strong_default,
            step=50 if strong_source == "NSE Stocks" else 25,
            key="strong_limit"
        )

        if st.button("🔥 RUN STRONG SIGNALS", key="run_strong_signals", type="primary", use_container_width=True):
            with st.spinner(f"Scanning {strong_source} for strong signals…"):
                universe = strong_universe_all if strong_limit == 0 else strong_universe_all[:strong_limit]
                if strong_source == "NSE Stocks":
                    r, e, s = run_nse_scan(fyers, universe)
                    st.session_state["nse_df"] = pd.DataFrame(r) if r else pd.DataFrame()
                    st.session_state["nse_errors"] = e
                    st.session_state["nse_stats"] = s
                else:
                    r, e, s = run_fo_scan(fyers, universe)
                    st.session_state["fo_df"] = pd.DataFrame(r) if r else pd.DataFrame()
                    st.session_state["fo_errors"] = e
                    st.session_state["fo_stats"] = s

        strong_df = st.session_state.get("nse_df" if strong_source == "NSE Stocks" else "fo_df")
        
        if strong_df is not None and not strong_df.empty:
            try:
                confidence_col = pd.to_numeric(strong_df.get("AI CONFIDENCE %", pd.Series([])), errors='coerce')
                strong_filtered = strong_df[confidence_col >= 75].copy()
                
                normalized = strong_filtered["AI SIGNAL"].apply(normalize_signal)
                strong_filtered = strong_filtered[normalized != "NEUTRAL"]
                
                st.subheader(f"💪 {len(strong_filtered)} Strong Signals")
                
                if len(strong_filtered) > 0:
                    st.dataframe(strong_filtered.sort_values("AI CONFIDENCE %", ascending=False), 
                               use_container_width=True, height=400)
                    _excel_download_button(
                        strong_filtered.sort_values("AI CONFIDENCE %", ascending=False),
                        "STRONG_SIGNALS",
                        "download_strong_signals_excel"
                    )
                else:
                    st.warning("No strong signals (≥75% confidence) found. Lower the threshold in Settings tab.")
            
            except Exception as e:
                st.error(f"❌ Error: {str(e)[:100]}")
        else:
            st.info(f"👈 Run '{strong_source}' scanner first")
    
    # ════════════════════════════════════════════════════════════════════════════════
    # TAB 6: SWING — CROSS + LONG-MOVE RADAR
    # ════════════════════════════════════════════════════════════════════════════════
    with tabs[7]:
        st.markdown("### 📈 Swing Trading — Long-Move Radar + Golden/Death Cross")
        st.caption("Daily closed-candle trend scanner. LONG-MOVE WATCH is a setup filter, not a guaranteed forecast.")

        col1, col2 = st.columns([3, 1])
        with col1:
            swing_limit = st.number_input("Scan limit (0 = ALL NSE)", min_value=10, max_value=len(all_symbols),
                                         value=min(100, len(all_symbols)), step=25, key="swing_limit")
        with col2:
            st.metric("Available", len(all_symbols))
        swing_symbols = all_symbols if swing_limit == 0 else all_symbols[:swing_limit]

        b1, b2 = st.columns(2)
        with b1:
            if st.button(f"🚀 RUN SWING LONG-MOVE ({len(swing_symbols)} stocks)", key="swing_long_run", type="primary", use_container_width=True):
                results = []
                prog = st.progress(0)
                with st.spinner("Scanning daily trend, EMA structure, momentum and volume…"):
                    for idx, symbol in enumerate(swing_symbols):
                        r = detect_swing_long_move(fyers, symbol)
                        # Keep both successful and failed rows so the user can see why a symbol was skipped.
                        results.append(r)
                        prog.progress((idx + 1) / max(len(swing_symbols),1))
                prog.empty()
                df_long = pd.DataFrame(results)
                if not df_long.empty:
                    usable = df_long[df_long["ltp"].notna()].copy()
                    if not usable.empty:
                        usable = usable.sort_values(["score","return60"], ascending=False, na_position="last")
                    st.session_state["swing_long_df"] = usable if not usable.empty else df_long
                    st.success(f"✅ Swing scan complete: {len(usable)} stocks with usable daily data out of {len(df_long)} scanned.")
                else:
                    st.warning("No symbols were scanned. Check the NSE symbol list / API connection.")
        with b2:
            if st.button(f"📈 DETECT CROSSOVERS ({len(swing_symbols)} stocks)", key="swing_run", use_container_width=True):
                swing_results = []
                swing_progress = st.progress(0)
                with st.spinner("Analyzing daily charts for Golden/Death Cross…"):
                    for idx, symbol in enumerate(swing_symbols):
                        try:
                            cc_data = detect_golden_death_cross(fyers, symbol)
                            if cc_data.get("reason") == "OK":
                                ticker = symbol.replace("NSE:", "").replace("-EQ", "")
                                swing_results.append({"Symbol": ticker, "LTP": cc_data.get("ltp", "N/A"), "EMA50": cc_data.get("ema50", "N/A"),
                                                      "EMA200": cc_data.get("ema200", "N/A"), "EMA Trend": cc_data.get("ema_trend", "N/A"),
                                                      "Signal": cc_data.get("signal", "NONE"), "Signal Date": cc_data.get("signal_date", "N/A")})
                        except Exception:
                            pass
                        swing_progress.progress((idx + 1) / max(len(swing_symbols),1))
                swing_progress.empty()
                if swing_results:
                    st.session_state["swing_df"] = pd.DataFrame(swing_results)

        long_df = st.session_state.get("swing_long_df")
        if long_df is not None and not long_df.empty:
            st.success(f"✅ Swing Long-Move scan complete: {len(long_df)} stocks")
            fcol1, fcol2 = st.columns(2)
            with fcol1:
                long_filter = st.selectbox("Long-Move Status", ["ALL", "🟢 LONG-MOVE WATCH", "🟡 EARLY WATCH", "⚪ WAIT"], key="swing_long_filter")
            with fcol2:
                trend_filter = st.selectbox("Daily Trend", ["ALL", "BULLISH", "NEUTRAL", "BEARISH"], key="swing_long_trend")
            view = _add_reversal_columns(long_df.copy())
            if long_filter != "ALL": view = view[view["status"] == long_filter]
            if trend_filter != "ALL": view = view[view["trend"] == trend_filter]
            st.dataframe(view.head(50), use_container_width=True, hide_index=True)
            _excel_download_button(view, "SWING_LONG_MOVE", "download_swing_long_move_excel")
            st.caption("Higher score means more conditions are aligned; it does not mean the stock will definitely rise.")
        else:
            st.info("👈 Click RUN SWING LONG-MOVE to find stocks with stronger daily trend/momentum alignment.")

        if long_df is not None and not long_df.empty and "reason" in long_df.columns:
            bad = long_df[long_df["ltp"].isna()].copy() if "ltp" in long_df.columns else pd.DataFrame()
            if not bad.empty:
                with st.expander(f"⚠️ Daily data unavailable / skipped: {len(bad)} stocks", expanded=False):
                    st.dataframe(bad[[c for c in ["symbol","status","reason"] if c in bad.columns]].head(100), use_container_width=True, hide_index=True)

        swing_df = st.session_state.get("swing_df")
        if swing_df is not None and not swing_df.empty:
            st.markdown("#### Golden / Death Cross results")
            swing_report = _add_reversal_columns(swing_df.copy())
            st.dataframe(swing_report, use_container_width=True, hide_index=True)
            _excel_download_button(swing_report, "SWING_CROSS_REVERSAL", "download_swing_cross_reversal_excel")

    # ════════════════════════════════════════════════════════════════════════════════
    # TAB 7: ADDITIONAL ANALYSIS
    # ════════════════════════════════════════════════════════════════════════════════
    with tabs[8]:
        st.markdown("### 🧠 Additional Analysis - Deep Dive on Single Stock")
        
        col1, col2 = st.columns(2)
        with col1:
            aa_universe = st.radio("Select Universe", ["NSE", "F&O"], horizontal=True, key="aa_universe")
            aa_symbols = all_symbols if aa_universe == "NSE" else fo_symbols
        
        with col2:
            aa_symbol_input = st.selectbox("Choose Stock", aa_symbols, key="aa_stock_select")
        
        if st.button("🔍 ANALYZE", key="aa_run"):
            with st.spinner(f"Analyzing {aa_symbol_input}…"):
                try:
                    analysis_5m = analyze_timeframe(fyers, aa_symbol_input, "5")
                    analysis_15m = analyze_timeframe(fyers, aa_symbol_input, "15")
                    analysis_1h = analyze_timeframe(fyers, aa_symbol_input, "60")
                    
                    if aa_universe == "F&O":
                        options_data = fetch_options_chain_data(fyers, aa_symbol_input)
                    else:
                        options_data = None
                    
                    master_signal = calculate_master_signal(aa_symbol_input, analysis_5m, analysis_15m, analysis_1h, options_data)
                    
                    st.session_state["aa_analysis"] = {
                        "5m": analysis_5m,
                        "15m": analysis_15m,
                        "1h": analysis_1h,
                        "master": master_signal,
                        "options": options_data,
                    }
                
                except Exception as e:
                    st.error(f"❌ Analysis failed: {str(e)}")
        
        aa_analysis = st.session_state.get("aa_analysis")
        if aa_analysis:
            ticker = aa_symbol_input.replace("NSE:", "").replace("-EQ", "")
            
            st.markdown(f"## {ticker} Analysis")
            master = aa_analysis.get("master", {})
            signal = master.get("final_signal", "NEUTRAL")
            conf = master.get("confidence", 0)
            
            if "BUY" in signal:
                st.success(f"{signal} — {conf:.0f}% Confidence")
            elif "SELL" in signal:
                st.error(f"{signal} — {conf:.0f}% Confidence")
            else:
                st.warning(f"{signal} — {conf:.0f}% Confidence")
            
            st.markdown("### ⏱️ Timeframe Analysis")
            col_5m, col_15m, col_1h = st.columns(3)
            
            with col_5m:
                d5 = aa_analysis["5m"].get("data", {})
                if d5:
                    st.write("**5 MINUTE**")
                    st.write(f"Trend: {d5.get('structure_trend', 'N/A')}")
                    st.write(f"Structure: {d5.get('structure_type', 'N/A')}")
                    st.write(f"LTP: {d5.get('last_close', 'N/A')}")
                    st.write(f"RSI: {d5.get('rsi', 'N/A')}")
                    st.write(f"RVOL: {d5.get('rvol', 'N/A')}x")
            
            with col_15m:
                d15 = aa_analysis["15m"].get("data", {})
                if d15:
                    st.write("**15 MINUTE**")
                    st.write(f"Trend: {d15.get('structure_trend', 'N/A')}")
                    st.write(f"Structure: {d15.get('structure_type', 'N/A')}")
                    st.write(f"VWAP: {d15.get('vwap', 'N/A')}")
                    st.write(f"EMA Trend: {d15.get('ema_trend', 'N/A')}")
            
            with col_1h:
                d1h = aa_analysis["1h"].get("data", {})
                if d1h:
                    st.write("**1 HOUR**")
                    st.write(f"Trend: {d1h.get('structure_trend', 'N/A')}")
                    st.write(f"Structure: {d1h.get('structure_type', 'N/A')}")
                    st.write(f"EMA50: {d1h.get('ema50', 'N/A')}")
                    st.write(f"EMA200: {d1h.get('ema200', 'N/A')}")
            
            st.markdown("### 📊 Pressure Analysis")
            if d5:
                bp = d5.get("buying_pressure", 0)
                sp = d5.get("selling_pressure", 0)
                col_bp1, col_bp2 = st.columns(2)
                with col_bp1:
                    st.metric("🟢 Buying Pressure", f"{bp}%")
                with col_bp2:
                    st.metric("🔴 Selling Pressure", f"{sp}%")
            
            st.markdown("### 🏗️ Market Structure")
            col_struct1, col_struct2, col_struct3 = st.columns(3)
            
            with col_struct1:
                if d5 and d5.get("bullish_choch"):
                    st.success("✅ Bullish CHoCH")
                elif d5 and d5.get("bearish_choch"):
                    st.error("❌ Bearish CHoCH")
            
            with col_struct2:
                if d5 and d5.get("bullish_mss"):
                    st.success("✅ Bullish MSS")
                elif d5 and d5.get("bearish_mss"):
                    st.error("❌ Bearish MSS")
            
            with col_struct3:
                if d5 and d5.get("bullish_cisd"):
                    st.success("✅ Bullish CISD")
                elif d5 and d5.get("bearish_cisd"):
                    st.error("❌ Bearish CISD")
            
            st.markdown("### 💹 Trade Plan")
            col_tp1, col_tp2, col_tp3, col_tp4 = st.columns(4)
            
            entry = master.get("entry", "N/A")
            sl = master.get("stop_loss", "N/A")
            t1 = master.get("target1", "N/A")
            t2 = master.get("target2", "N/A")
            rr = master.get("rr_ratio", "N/A")
            
            with col_tp1:
                st.metric("Entry", f"{entry}")
            with col_tp2:
                st.metric("Stop Loss", f"{sl}")
            with col_tp3:
                st.metric("Target 1", f"{t1}")
            with col_tp4:
                st.metric("Target 2 / R:R", f"{t2} / {rr}")
            
            st.markdown("### 📝 Signal Explanation")
            reason = master.get("signal_reason", "N/A")
            st.info(reason)
            
            if aa_universe == "F&O" and aa_analysis.get("options"):
                st.markdown("### 📊 Options Analysis")
                opt = aa_analysis["options"]
                col_opt1, col_opt2, col_opt3, col_opt4 = st.columns(4)
                
                with col_opt1:
                    st.metric("ATM Strike", opt.get("atm_strike", "N/A"))
                with col_opt2:
                    st.metric("PCR", opt.get("pcr", "N/A"))
                with col_opt3:
                    st.metric("CE OI", opt.get("ce_oi", "N/A"))
                with col_opt4:
                    st.metric("Options Bias", opt.get("options_bias", "N/A"))
    
    # ════════════════════════════════════════════════════════════════════════════════
    # TAB 8: MARKET DASHBOARD
    # ════════════════════════════════════════════════════════════════════════════════
    with tabs[9]:
        st.markdown("### 📊 Market Dashboard - Statistics & Sentiment")
        dashboard_source = st.radio("Data Source", ["NSE Stocks", "F&O Stocks"], horizontal=True, key="dash_source")

        dash_universe_all = all_symbols if dashboard_source == "NSE Stocks" else fo_symbols
        dash_default = min(500 if dashboard_source == "NSE Stocks" else 200, len(dash_universe_all))
        dash_limit = st.number_input(
            "Dashboard scan limit (0 = all)",
            min_value=0, max_value=len(dash_universe_all),
            value=dash_default,
            step=50 if dashboard_source == "NSE Stocks" else 25,
            key="dash_limit"
        )

        if st.button("📊 RUN MARKET DASHBOARD", key="run_market_dashboard", type="primary", use_container_width=True):
            with st.spinner(f"Scanning {dashboard_source} for market dashboard…"):
                universe = dash_universe_all if dash_limit == 0 else dash_universe_all[:dash_limit]
                if dashboard_source == "NSE Stocks":
                    r, e, s = run_nse_scan(fyers, universe)
                    st.session_state["nse_df"] = pd.DataFrame(r) if r else pd.DataFrame()
                    st.session_state["nse_errors"] = e
                    st.session_state["nse_stats"] = s
                else:
                    r, e, s = run_fo_scan(fyers, universe)
                    st.session_state["fo_df"] = pd.DataFrame(r) if r else pd.DataFrame()
                    st.session_state["fo_errors"] = e
                    st.session_state["fo_stats"] = s

        dash_df = st.session_state.get("nse_df" if dashboard_source == "NSE Stocks" else "fo_df")
        
        if dash_df is not None and not dash_df.empty:
            stats = calculate_market_stats(dash_df)
            
            st.markdown("### 📈 Market Overview")
            col_ov1, col_ov2, col_ov3, col_ov4, col_ov5 = st.columns(5)
            
            with col_ov1:
                st.metric("Total Scanned", stats["total"])
            with col_ov2:
                st.metric("🟢 BUY", stats["buy"])
            with col_ov3:
                st.metric("🔴 SELL", stats["sell"])
            with col_ov4:
                st.metric("🟡 NEUTRAL", stats["neutral"])
            with col_ov5:
                st.metric("Avg Confidence", f"{stats['avg_confidence']:.1f}%")
            
            st.markdown("### 💪 Strong Signals")
            col_str1, col_str2 = st.columns(2)
            
            with col_str1:
                st.metric("💪 Strong BUY", stats["strong_buy"])
            with col_str2:
                st.metric("💪 Strong SELL", stats["strong_sell"])
            
            st.markdown("### 😊 Market Sentiment")
            col_sent1, col_sent2, col_sent3 = st.columns(3)
            
            with col_sent1:
                st.metric("Bullish %", f"{stats['buy_pct']:.1f}%")
            with col_sent2:
                st.metric("Bearish %", f"{stats['sell_pct']:.1f}%")
            with col_sent3:
                st.metric("Neutral %", f"{stats['neutral_pct']:.1f}%")

            _excel_download_button(
                dash_df,
                "MARKET_DASHBOARD",
                "download_market_dashboard_excel"
            )
        else:
            st.info(f"👈 Run '{dashboard_source}' scanner first")
    
    # ════════════════════════════════════════════════════════════════════════════════
    # TAB 9: SETTINGS
    # ════════════════════════════════════════════════════════════════════════════════
    with tabs[10]:
        st.markdown("### ⚙️ Scanner Settings & Configuration")
        
        st.markdown("#### 🎯 Signal Filtering")
        col_set1, col_set2, col_set3 = st.columns(3)
        
        with col_set1:
            default_conf = st.number_input("Default Min Confidence %", 0, 100, DEFAULT_CONFIDENCE_THRESHOLD, 5, key="set_conf")
        with col_set2:
            default_rvol = st.slider("Default Min RVOL", 0.5, 3.0, DEFAULT_RVOL_THRESHOLD, 0.1, key="set_rvol")
        with col_set3:
            default_strong_rvol = st.slider("Strong Signal RVOL", 1.0, 3.0, DEFAULT_STRONG_RVOL, 0.1, key="set_strong_rvol")
        
        st.markdown("#### ⚡ Live Movement Settings")
        col_mom1, col_mom2, col_mom3 = st.columns(3)
        
        with col_mom1:
            momentum_min_score = st.slider("Min Live Movement Score", 50, 100, MOMENTUM_MIN_SCORE, 5, key="set_mom_score")
        with col_mom2:
            momentum_strong_score = st.slider("Strong BIG MOVE Score", 75, 100, MOMENTUM_STRONG_SCORE, 5, key="set_mom_strong")
        with col_mom3:
            momentum_min_rvol = st.slider("Min Live Movement RVOL", 1.0, 3.0, MOMENTUM_MIN_RVOL, 0.1, key="set_mom_rvol")
        
        st.markdown("#### ℹ️ Information")
        st.info("""
        **NSE AI PRO V17 — Features:**
        - ✅ Multi-timeframe analysis (5M, 15M, 1H, Daily)
        - ✅ Strict signal validation engine
        - ✅ Pressure-based confirmation
        - ✅ VWAP, EMA, RSI, MACD indicators
        - ✅ Market structure (CHoCH, MSS, CISD)
        - ✅ Options chain analysis (F&O)
        - ✅ Golden Cross / Death Cross detection
        - ✅ **⚡ NEW: MOMENTUM MOVERS Scanner**
        - ✅ **📖 NEW: LIVE FYERS EXCHANGE ORDER BOOK**
        - ✅ Next Candle Bias + LIVE Depth Direction + BUY/SELL PIN + Buy/Sell Liquidity
        
        **Data Source:** Fyers Live API
        **Timeframes:** 5M, 15M, 1H, Daily
        **Universes:** NSE Equities + F&O Stocks
        """)
    
    # ════════════════════════════════════════════════════════════════════════════════
    # TAB 10: PIN RULES — ADDITIONAL ONLY
    # ════════════════════════════════════════════════════════════════════════════════
    with tabs[11]:
        _show_pin_rules_tab(fyers, all_symbols, fo_symbols)
    
    # ════════════════════════════════════════════════════════════════════════════════
    # TAB 11: F&O OPTION CHECK — LIVE RUN
    # ════════════════════════════════════════════════════════════════════════════════
    with tabs[12]:
        st.markdown("### 🎯 F&O OPTION CHECK")
        st.caption("RUN LIVE CHECK fetches the current FYERS option-chain data. CE/PE is shown as WATCH, not an order instruction.")
        fo_pick = st.selectbox("Select F&O stock", fo_symbols if fo_symbols else all_symbols, key="fo_option_check_symbol")
        c1, c2, c3 = st.columns(3)
        with c1:
            strike_input = st.number_input("Strike", min_value=0.0, value=1150.0, step=50.0, key="fo_strike_example")
        with c2:
            option_side = st.selectbox("Option side", ["CE", "PE"], key="fo_option_side")
        with c3:
            st.write("")
            run_live = st.button("▶ RUN LIVE OPTION CHECK", key="fo_option_live_run", type="primary", use_container_width=True)

        if run_live:
            with st.spinner(f"Fetching live option chain for {fo_pick}…"):
                live_opt = fetch_options_chain_data(fyers, fo_pick)
            st.session_state["fo_option_live"] = live_opt
            st.session_state["fo_option_live_symbol"] = fo_pick

        live_opt = st.session_state.get("fo_option_live")
        if live_opt and st.session_state.get("fo_option_live_symbol") != fo_pick:
            st.info("Selected stock changed. Press RUN LIVE OPTION CHECK to fetch this stock's chain.")
        elif live_opt:
            if live_opt.get("status") == "OK":
                spot = live_opt.get("spot")
                if spot is None: spot = 0.0
                if option_side == "CE":
                    moneyness = "ITM" if spot > strike_input else ("ATM" if spot == strike_input else "OTM")
                    intrinsic = max(spot-strike_input,0)
                else:
                    moneyness = "ITM" if spot < strike_input else ("ATM" if spot == strike_input else "OTM")
                    intrinsic = max(strike_input-spot,0)
                m1,m2,m3,m4 = st.columns(4)
                m1.metric("LIVE SPOT / LTP", f"₹{spot:,.2f}")
                m2.metric("STRIKE", f"₹{strike_input:,.2f}")
                m3.metric("MONEYNESS", moneyness)
                m4.metric("INTRINSIC", f"₹{intrinsic:,.2f}")
                s1,s2,s3,s4 = st.columns(4)
                s1.metric("PCR", f"{live_opt.get('pcr'):.2f}" if live_opt.get('pcr') is not None else "—")
                s2.metric("CE VOLUME", f"{live_opt.get('ce_volume',0):,.0f}")
                s3.metric("PE VOLUME", f"{live_opt.get('pe_volume',0):,.0f}")
                s4.metric("OPTIONS BIAS", str(live_opt.get('options_bias','—')))
                chain = live_opt.get("chain") or []
                if chain:
                    rows=[]
                    for x in chain:
                        try:
                            k=float(x.get("strike_price")); typ=x.get("option_type")
                            if abs(k-strike_input) <= max(1.0, abs(strike_input)*0.08):
                                rows.append({"Strike":k,"Type":typ,"LTP":x.get("ltp"),"OI":x.get("oi"),"OI Change":x.get("oich"),"Volume":x.get("volume"),"IV":x.get("iv")})
                        except Exception: pass
                    if rows:
                        st.markdown("#### Nearby option strikes")
                        st.dataframe(pd.DataFrame(rows).sort_values(["Strike","Type"]), use_container_width=True, hide_index=True)
                st.info("Use the live stock direction/PIN/order-flow tabs as confirmation. Option-chain data alone should not be treated as a guaranteed direction signal.")
            else:
                st.error(f"Option-chain data unavailable: {live_opt.get('message','Unknown error')}")
        else:
            st.info("👆 Select an F&O stock and click RUN LIVE OPTION CHECK to refresh the current spot and option-chain data.")

    # ════════════════════════════════════════════════════════════════════════════════
    # TAB 13: DIRECT REVERSAL SCANNER — RUN WITHOUT PRE-SCANNING NSE/F&O
    # ════════════════════════════════════════════════════════════════════════════════
    with tabs[13]:
        st.markdown("### 🔄 REVERSAL SCANNER")
        st.caption(
            "Direct reversal scan for NSE / F&O / BOTH. "
            "This tab runs the required scan itself — no need to run NSE/F&O first."
        )

        rev_source = st.radio(
            "Source",
            ["NSE STOCKS", "F&O STOCKS", "BOTH"],
            horizontal=True,
            key="direct_reversal_source",
        )

        rev_c1, rev_c2 = st.columns(2)
        with rev_c1:
            rev_nse_limit = st.number_input(
                "NSE Scan Limit (0 = ALL)",
                min_value=0,
                max_value=len(all_symbols),
                value=min(500, len(all_symbols)),
                step=50,
                key="direct_reversal_nse_limit",
            )
        with rev_c2:
            rev_fo_limit = st.number_input(
                "F&O Scan Limit (0 = ALL)",
                min_value=0,
                max_value=len(fo_symbols),
                value=min(200, len(fo_symbols)),
                step=25,
                key="direct_reversal_fo_limit",
            )

        rev_nse_symbols = all_symbols if rev_nse_limit == 0 else all_symbols[:rev_nse_limit]
        rev_fo_symbols = fo_symbols if rev_fo_limit == 0 else fo_symbols[:rev_fo_limit]

        if st.button(
            "🔄 RUN REVERSAL SCAN",
            key="direct_reversal_run",
            type="primary",
            use_container_width=True,
        ):
            reversal_frames = []
            reversal_errors = []
            reversal_stats = []

            with st.spinner("🔄 Running direct reversal scan…"):
                # NSE scan
                if rev_source in ["NSE STOCKS", "BOTH"]:
                    try:
                        nse_results, nse_errors, nse_stats = run_nse_scan(
                            fyers, rev_nse_symbols
                        )
                        reversal_errors.extend(nse_errors or [])
                        if nse_stats:
                            reversal_stats.append(("NSE", nse_stats))
                        if nse_results:
                            nse_rev = pd.DataFrame(nse_results)
                            if not nse_rev.empty:
                                nse_rev = _add_reversal_columns(nse_rev)
                                nse_rev["SOURCE"] = "NSE"
                                reversal_frames.append(nse_rev)
                    except Exception as e:
                        reversal_errors.append(
                            f"NSE direct reversal scan error: {type(e).__name__}: {str(e)[:180]}"
                        )

                # F&O scan
                if rev_source in ["F&O STOCKS", "BOTH"]:
                    try:
                        fo_results, fo_errors, fo_stats = run_fo_scan(
                            fyers, rev_fo_symbols
                        )
                        reversal_errors.extend(fo_errors or [])
                        if fo_stats:
                            reversal_stats.append(("F&O", fo_stats))
                        if fo_results:
                            fo_rev = pd.DataFrame(fo_results)
                            if not fo_rev.empty:
                                fo_rev = _add_reversal_columns(fo_rev)
                                fo_rev["SOURCE"] = "F&O"
                                reversal_frames.append(fo_rev)
                    except Exception as e:
                        reversal_errors.append(
                            f"F&O direct reversal scan error: {type(e).__name__}: {str(e)[:180]}"
                        )

            if reversal_frames:
                reversal_df = pd.concat(reversal_frames, ignore_index=True)

                if "REVERSAL SCORE" in reversal_df.columns:
                    reversal_df["REVERSAL SCORE"] = pd.to_numeric(
                        reversal_df["REVERSAL SCORE"], errors="coerce"
                    ).fillna(0)

                # Strongest reversal setups first.
                sort_cols = [c for c in ["REVERSAL SCORE", "AI CONFIDENCE %"] if c in reversal_df.columns]
                if sort_cols:
                    reversal_df = reversal_df.sort_values(
                        sort_cols,
                        ascending=[False] * len(sort_cols),
                        na_position="last",
                    )
            else:
                reversal_df = pd.DataFrame()

            st.session_state["direct_reversal_df"] = reversal_df
            st.session_state["direct_reversal_errors"] = reversal_errors
            st.session_state["direct_reversal_scanned_at"] = _generated_timestamp()
            st.session_state["direct_reversal_stats"] = reversal_stats

            if reversal_df.empty:
                st.warning("⚠️ No reversal data returned. Check the FYERS API/data availability.")
            else:
                st.success(
                    f"✅ Direct reversal scan complete — {len(reversal_df)} stocks analyzed."
                )

        reversal_df = st.session_state.get("direct_reversal_df")

        if isinstance(reversal_df, pd.DataFrame) and not reversal_df.empty:
            # Normalize symbol/name for display.
            display_df = reversal_df.copy()
            symbol_col = next(
                (c for c in ["STOCK NAME", "SYMBOL", "Symbol", "symbol"] if c in display_df.columns),
                None,
            )
            if symbol_col and symbol_col != "STOCK NAME":
                display_df = display_df.rename(columns={symbol_col: "STOCK NAME"})
            if "STOCK NAME" not in display_df.columns:
                display_df.insert(0, "STOCK NAME", "N/A")

            # Summary counts.
            signal_text = (
                display_df["REVERSAL SIGNAL"].astype(str).str.upper()
                if "REVERSAL SIGNAL" in display_df.columns
                else pd.Series("WAIT", index=display_df.index)
            )
            buy_count = int(signal_text.eq("BUY REVERSAL").sum())
            sell_count = int(signal_text.eq("SELL REVERSAL").sum())
            wait_count = int(signal_text.eq("WAIT").sum())

            st.markdown("### 🎯 REVERSAL RESULTS")
            s1, s2, s3, s4 = st.columns(4)
            s1.metric("📊 TOTAL", len(display_df))
            s2.metric("🟢 BUY REVERSAL", buy_count)
            s3.metric("🔴 SELL REVERSAL", sell_count)
            s4.metric("⏳ WAIT", wait_count)

            # User-selectable minimum reversal score.
            min_rev_score = st.slider(
                "Minimum Reversal Score",
                min_value=0,
                max_value=100,
                value=50,
                step=5,
                key="direct_reversal_min_score",
            )

            view = display_df.copy()
            if "REVERSAL SCORE" in view.columns:
                score = pd.to_numeric(view["REVERSAL SCORE"], errors="coerce").fillna(0)
                view = view[score >= min_rev_score].copy()

            # Put actionable reversal columns first, while retaining all data.
            preferred = [
                "STOCK NAME", "SOURCE", "LTP", "DIRECTION",
                "REVERSAL SIGNAL", "REVERSAL SCORE", "REVERSAL LEVEL",
                "REVERSAL ZONE", "REVERSAL REASON", "RSI", "RVOL", "VWAP",
                "AI SIGNAL", "AI CONFIDENCE %", "REASON",
            ]
            preferred_existing = [c for c in preferred if c in view.columns]
            remaining = [c for c in view.columns if c not in preferred_existing]
            view = view[preferred_existing + remaining]

            # FULL REPORT — ALWAYS SHOW ALL SCANNED ROWS.
            # The score slider controls only the Watch List below.
            st.markdown(f"### 📋 FULL REVERSAL REPORT ({len(display_df)})")
            st.dataframe(
                display_df,
                use_container_width=True,
                height=560,
                hide_index=True,
            )

            r1, r2, r3 = st.columns(3)
            with r1:
                _excel_download_button(
                    display_df,
                    "DIRECT_REVERSAL_FULL_REPORT",
                    "direct_reversal_full_excel",
                    label="📊 Download Full Excel",
                )
            with r2:
                try:
                    st.download_button(
                        "📄 Full CSV",
                        to_csv_bytes(display_df),
                        f"DIRECT_REVERSAL_FULL_{_now_ist().strftime('%Y%m%d_%H%M')}.csv",
                        "text/csv",
                        key="direct_reversal_full_csv",
                        use_container_width=True,
                    )
                except Exception as e:
                    st.error(f"❌ Full CSV export failed: {str(e)[:100]}")
            with r3:
                try:
                    st.download_button(
                        "📋 Full JSON",
                        to_json_bytes(display_df),
                        f"DIRECT_REVERSAL_FULL_{_now_ist().strftime('%Y%m%d_%H%M')}.json",
                        "application/json",
                        key="direct_reversal_full_json",
                        use_container_width=True,
                    )
                except Exception as e:
                    st.error(f"❌ Full JSON export failed: {str(e)[:100]}")

            # FILTERED WATCH LIST — SCORE FILTER ONLY.
            st.markdown(f"### 🔥 REVERSAL WATCH LIST ({len(view)})")
            if view.empty:
                st.info(
                    f"No rows meet the selected minimum reversal score ({min_rev_score}). "
                    "The FULL REVERSAL REPORT above still contains every scanned stock."
                )
            else:
                st.dataframe(
                    view,
                    use_container_width=True,
                    height=560,
                    hide_index=True,
                )

                d1, d2, d3 = st.columns(3)
                with d1:
                    _excel_download_button(
                        view,
                        "DIRECT_REVERSAL",
                        "direct_reversal_excel",
                        label="📊 Excel",
                    )
                with d2:
                    try:
                        st.download_button(
                            "📄 CSV",
                            to_csv_bytes(view),
                            f"DIRECT_REVERSAL_{_now_ist().strftime('%Y%m%d_%H%M')}.csv",
                            "text/csv",
                            key="direct_reversal_csv",
                            use_container_width=True,
                        )
                    except Exception as e:
                        st.error(f"❌ CSV export failed: {str(e)[:100]}")
                with d3:
                    try:
                        st.download_button(
                            "📋 JSON",
                            to_json_bytes(view),
                            f"DIRECT_REVERSAL_{_now_ist().strftime('%Y%m%d_%H%M')}.json",
                            "application/json",
                            key="direct_reversal_json",
                            use_container_width=True,
                        )
                    except Exception as e:
                        st.error(f"❌ JSON export failed: {str(e)[:100]}")

            st.caption(
                f"Last direct reversal scan: "
                f"{st.session_state.get('direct_reversal_scanned_at', 'N/A')}"
            )

        else:
            st.info("👆 Select NSE/F&O/BOTH and click **RUN REVERSAL SCAN**.")

        direct_reversal_errors = st.session_state.get("direct_reversal_errors", [])
        if direct_reversal_errors:
            with st.expander(
                f"⚠️ {len(direct_reversal_errors)} scan errors",
                expanded=False,
            ):
                for err in direct_reversal_errors[:100]:
                    st.write(f"• {err}")
                if len(direct_reversal_errors) > 100:
                    st.caption("Showing first 100 errors.")

    # ════════════════════════════════════════════════════════════════════════════════
    # TAB 14: AI CHART ANALYSIS — MUST RENDER INSIDE show_scanner
    # ════════════════════════════════════════════════════════════════════════════════
    with tabs[14]:
        _show_ai_chart_analysis_tab()

    # ════════════════════════════════════════════════════════════════════════════════
    # TAB 15: ADDITIVE PRE-MOVE CONFIRMATION SCANNER (F&O universe)
    # ════════════════════════════════════════════════════════════════════════════════
    with tabs[15]:
        st.markdown("### 🎯 Intraday Pre-Move Confirmation — F&O Stocks")
        st.caption("Early watch → liquidity sweep → reversal → breakout → pullback. Existing scanner tabs and signals are unchanged.")
        st.warning("Setup Score అనేది probability కాదు. Confirmed signal కూడా guaranteed profit కాదు; paper-trading/backtest తప్పనిసరి.")
        scan_limit = st.number_input("F&O scan limit (0 = all; start with 30–50)", min_value=0, max_value=max(len(fo_symbols), 1), value=min(40, len(fo_symbols)), step=10, key="premove_confirm_limit")
        scan_universe = fo_symbols if scan_limit == 0 else fo_symbols[:int(scan_limit)]
        if st.button("▶ RUN PRE-MOVE CONFIRM SCAN", key="premove_confirm_run", type="primary"):
            rows, errors = [], []
            bar = st.progress(0.0, text=f"Fetching 5M/15M candles: 0 / {len(scan_universe)}")
            for ix, symbol in enumerate(scan_universe, start=1):
                try:
                    df5 = _fetch_timeframe_data(fyers, symbol, "5", lookback_days=5)
                    df15 = _fetch_timeframe_data(fyers, symbol, "15", lookback_days=10)
                    if df5 is None or df15 is None:
                        errors.append(f"{symbol}: candle data unavailable")
                        continue
                    sig = _intraday_pre_move_confirmation(df5, df15)
                    rows.append({"Symbol": symbol.replace("NSE:", "").replace("-EQ", ""),
                                 "LTP": round(float(df5["Close"].iloc[-1]), 2),
                                 "Signal Time": str(df5["Time"].iloc[-1]) if "Time" in df5.columns else _now_ist().strftime("%Y-%m-%d %H:%M:%S"),
                                 **sig})
                except Exception as exc:
                    errors.append(f"{symbol}: {type(exc).__name__}")
                bar.progress(ix / max(len(scan_universe), 1), text=f"Pre-move scan: {ix} / {len(scan_universe)}")
            bar.empty()
            result_df = pd.DataFrame(rows)
            if not result_df.empty:
                result_df["Setup Score"] = pd.to_numeric(result_df["Setup Score"], errors="coerce").fillna(0)
                result_df = result_df.sort_values(["Final Signal", "Setup Score"], ascending=[True, False]).reset_index(drop=True)
            st.session_state["premove_confirm_df"] = result_df
            st.session_state["premove_confirm_errors"] = errors
            st.session_state["premove_confirm_scanned_at"] = _now_ist().strftime("%Y-%m-%d %H:%M:%S IST")

        result_df = st.session_state.get("premove_confirm_df", pd.DataFrame())
        if isinstance(result_df, pd.DataFrame) and not result_df.empty:
            st.caption(f"Last scan: {st.session_state.get('premove_confirm_scanned_at', 'N/A')} | Rows: {len(result_df)}")
            show_only_confirmed = st.checkbox("Show only CONFIRMED BUY/SELL", value=False, key="premove_confirm_only")
            display_df = result_df.copy()
            if show_only_confirmed:
                display_df = display_df[display_df["Final Signal"].astype(str).str.startswith("CONFIRMED")]
            st.dataframe(display_df, use_container_width=True, height=520, hide_index=True)
            c1, c2 = st.columns(2)
            with c1:
                st.download_button("📊 Download Excel", _format_excel_output(display_df, "PRE_MOVE_CONFIRM"),
                                   f"NSE_FO_PREMOVE_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx",
                                   "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="premove_confirm_xlsx")
            with c2:
                st.download_button("📄 Download CSV", to_csv_bytes(display_df),
                                   f"NSE_FO_PREMOVE_{_now_ist().strftime('%Y%m%d_%H%M')}.csv",
                                   "text/csv", key="premove_confirm_csv")
        else:
            st.info("Click RUN PRE-MOVE CONFIRM SCAN. Start with 30–50 symbols to reduce API load.")
        errs = st.session_state.get("premove_confirm_errors", [])
        if errs:
            with st.expander(f"⚠️ Data/API issues ({len(errs)})"):
                st.write("\n".join(errs[:100]))

    with tabs[16]:
        _new_scan_tab(fyers, all_symbols, fo_symbols, "breakout")

    with tabs[17]:
        _new_scan_tab(fyers, all_symbols, fo_symbols, "amd")

    # TAB 18: ADDITIONAL INDICATORS — independent; does not alter existing scanners.
    with tabs[18]:
        st.markdown("### 📊 Additional Indicators — EMA Crossover + Momentum")
        st.caption("Uses candle data from FYERS. Signals are technical conditions, not guaranteed predictions.")
        src_col, tf_col, lim_col = st.columns(3)
        with src_col:
            ind_source = st.selectbox("Universe", ["F&O Stocks", "NSE Stocks"], key="additional_ind_source")
        with tf_col:
            ind_tf_label = st.selectbox("Timeframe", ["5 Minute", "15 Minute", "Daily"], index=1, key="additional_ind_tf")
        with lim_col:
            ind_symbols_all = fo_symbols if ind_source == "F&O Stocks" else all_symbols
            ind_limit = st.number_input("Scan limit (0 = ALL)", min_value=0,
                                        max_value=max(len(ind_symbols_all), 1),
                                        value=min(50, len(ind_symbols_all)), step=25,
                                        key="additional_ind_limit")
        ind_resolution = {"5 Minute": "5", "15 Minute": "15", "Daily": "1D"}[ind_tf_label]
        ind_universe = ind_symbols_all if int(ind_limit) == 0 else ind_symbols_all[:int(ind_limit)]
        st.info(f"Universe: {ind_source} | Timeframe: {ind_tf_label} | Selected: {len(ind_universe)}")
        st.markdown("""
        **Indicator rules**
        - **EMA 9/21 CROSS:** fresh bullish or bearish crossover on the latest candle.
        - **EMA 50/200:** longer trend alignment (Golden/Death Cross when fresh).
        - **RSI 14:** momentum; 70+ overbought, 30− oversold.
        - **MACD:** MACD line vs signal line and fresh crossover.
        - **VWAP:** session VWAP for intraday charts; shows whether price is above/below fair intraday average.
        - **VWAP GAP %:** percentage distance between LTP and VWAP; large positive/negative distance warns that price may be extended.
        - **Fair Value Gap (FVG):** three-candle imbalance zone (bullish: current Low > High two candles ago; bearish: current High < Low two candles ago). This is a price-imbalance estimate, not proof of an unfilled institutional order.
        - **FVG DISTANCE %:** distance from LTP to the nearest detected recent FVG zone.
        - **RVOL:** latest volume compared with the preceding 20-candle average.
        """)
        if st.button("▶ RUN ADDITIONAL INDICATORS", key="additional_ind_run", type="primary"):
            ind_rows, ind_errors = [], []
            ind_bar = st.progress(0.0, text=f"Scanning indicator candles: 0 / {len(ind_universe)}")
            for ind_i, ind_symbol in enumerate(ind_universe, start=1):
                try:
                    ind_df = _fetch_timeframe_data(
                        fyers, ind_symbol, ind_resolution,
                        lookback_days=260 if ind_tf_label == "Daily" else 5
                    )
                    if ind_df is None or len(ind_df) < 35:
                        ind_errors.append(f"{ind_symbol}: insufficient candle data")
                        continue
                    d = ind_df.copy().reset_index(drop=True)
                    for col in ["Open", "High", "Low", "Close", "Volume"]:
                        d[col] = pd.to_numeric(d[col], errors="coerce")
                    d = d.dropna(subset=["Close", "Volume"]).reset_index(drop=True)
                    if len(d) < 35:
                        ind_errors.append(f"{ind_symbol}: insufficient clean candles")
                        continue
                    close, volume = d["Close"], d["Volume"]
                    e9, e21 = calculate_ema(close, 9), calculate_ema(close, 21)
                    e50, e200 = calculate_ema(close, 50), calculate_ema(close, 200)
                    rsi_s = calculate_rsi(close, 14)
                    macd_line, macd_sig, macd_hist = calculate_macd(close)
                    # Intraday VWAP must reset each trading session; cumulative VWAP across
                    # several downloaded days can distort the current session reading.
                    if "Time" in d.columns and ind_tf_label != "Daily":
                        session_key = pd.to_datetime(d["Time"]).dt.strftime("%Y-%m-%d")
                        typical_price = (d["High"].astype(float) + d["Low"].astype(float) + close.astype(float)) / 3.0
                        pv = typical_price * volume.astype(float)
                        session_pv = pv.groupby(session_key).cumsum()
                        session_vol = volume.astype(float).groupby(session_key).cumsum().replace(0, np.nan)
                        vwap_s = (session_pv / session_vol).fillna(close)
                    else:
                        vwap_s = calculate_vwap(d)
                    vol_base = volume.shift(1).rolling(20, min_periods=5).mean()
                    rvol = float(volume.iloc[-1] / vol_base.iloc[-1]) if pd.notna(vol_base.iloc[-1]) and vol_base.iloc[-1] > 0 else 0.0
                    up_921 = e9.iloc[-2] <= e21.iloc[-2] and e9.iloc[-1] > e21.iloc[-1]
                    dn_921 = e9.iloc[-2] >= e21.iloc[-2] and e9.iloc[-1] < e21.iloc[-1]
                    up_50200 = e50.iloc[-2] <= e200.iloc[-2] and e50.iloc[-1] > e200.iloc[-1]
                    dn_50200 = e50.iloc[-2] >= e200.iloc[-2] and e50.iloc[-1] < e200.iloc[-1]
                    up_macd = macd_line.iloc[-2] <= macd_sig.iloc[-2] and macd_line.iloc[-1] > macd_sig.iloc[-1]
                    dn_macd = macd_line.iloc[-2] >= macd_sig.iloc[-2] and macd_line.iloc[-1] < macd_sig.iloc[-1]
                    trend = "BULLISH" if e9.iloc[-1] > e21.iloc[-1] else "BEARISH" if e9.iloc[-1] < e21.iloc[-1] else "NEUTRAL"
                    macd_state = "BULLISH" if macd_line.iloc[-1] > macd_sig.iloc[-1] else "BEARISH"
                    rsi = float(rsi_s.iloc[-1])
                    close_last = float(close.iloc[-1])
                    vwap_last = float(vwap_s.iloc[-1]) if pd.notna(vwap_s.iloc[-1]) else close_last
                    vwap_gap_pct = ((close_last - vwap_last) / vwap_last * 100.0) if vwap_last else 0.0
                    vwap_position = "ABOVE" if close_last > vwap_last else "BELOW" if close_last < vwap_last else "AT VWAP"

                    # Find the most recent three-candle Fair Value Gap in the last 30 bars.
                    fvg_type, fvg_low, fvg_high, fvg_idx = "NONE", np.nan, np.nan, None
                    fvg_start = max(2, len(d) - 30)
                    for fi in range(len(d) - 1, fvg_start - 1, -1):
                        candle_now = d.iloc[fi]
                        candle_two_back = d.iloc[fi - 2]
                        if float(candle_now["Low"]) > float(candle_two_back["High"]):
                            fvg_type = "BULLISH FVG"
                            fvg_low, fvg_high, fvg_idx = float(candle_two_back["High"]), float(candle_now["Low"]), fi
                            break
                        if float(candle_now["High"]) < float(candle_two_back["Low"]):
                            fvg_type = "BEARISH FVG"
                            fvg_low, fvg_high, fvg_idx = float(candle_now["High"]), float(candle_two_back["Low"]), fi
                            break
                    if fvg_type == "NONE":
                        fvg_distance_pct = np.nan
                        fvg_relation = "NO RECENT FVG"
                    else:
                        if fvg_low <= close_last <= fvg_high:
                            fvg_distance_pct = 0.0
                            fvg_relation = "INSIDE FVG"
                        else:
                            nearest_fvg_edge = fvg_low if close_last < fvg_low else fvg_high
                            fvg_distance_pct = abs(close_last - nearest_fvg_edge) / close_last * 100.0 if close_last else np.nan
                            fvg_relation = "BELOW FVG" if close_last < fvg_low else "ABOVE FVG"
                    # Gap zone may be stale/filled; expose the bar age so it can be reviewed.
                    fvg_bars_ago = (len(d) - 1 - fvg_idx) if fvg_idx is not None else np.nan
                    signal_parts = []
                    if up_921: signal_parts.append("🟢 EMA 9/21 BULL CROSS")
                    elif dn_921: signal_parts.append("🔴 EMA 9/21 BEAR CROSS")
                    elif trend == "BULLISH": signal_parts.append("BULLISH EMA ALIGNMENT")
                    elif trend == "BEARISH": signal_parts.append("BEARISH EMA ALIGNMENT")
                    if up_50200: signal_parts.append("🟢 GOLDEN CROSS 50/200")
                    elif dn_50200: signal_parts.append("🔴 DEATH CROSS 50/200")
                    if up_macd: signal_parts.append("MACD BULL CROSS")
                    elif dn_macd: signal_parts.append("MACD BEAR CROSS")
                    if close_last > vwap_last and vwap_gap_pct >= 0:
                        signal_parts.append("PRICE ABOVE VWAP")
                    elif close_last < vwap_last:
                        signal_parts.append("PRICE BELOW VWAP")
                    if abs(vwap_gap_pct) >= 1.0:
                        signal_parts.append("⚠️ FAR FROM VWAP")
                    if fvg_type != "NONE":
                        signal_parts.append(fvg_type)
                    if rvol >= 1.5: signal_parts.append("VOLUME SPIKE")
                    if not signal_parts: signal_parts.append("WAIT / NO FRESH CROSS")
                    ind_rows.append({
                        "Symbol": ind_symbol.replace("NSE:", "").replace("-EQ", ""),
                        "Timeframe": ind_tf_label,
                        "Signal Time": str(d["Time"].iloc[-1]) if "Time" in d.columns else "N/A",
                        "LTP": round(close_last, 2),
                        "EMA 9": round(float(e9.iloc[-1]), 2),
                        "EMA 21": round(float(e21.iloc[-1]), 2),
                        "EMA 9/21 Cross": "BULLISH CROSS" if up_921 else "BEARISH CROSS" if dn_921 else "NONE",
                        "EMA 50": round(float(e50.iloc[-1]), 2),
                        "EMA 200": round(float(e200.iloc[-1]), 2),
                        "50/200 Cross": "GOLDEN CROSS" if up_50200 else "DEATH CROSS" if dn_50200 else "NONE",
                        "EMA Trend": trend,
                        "RSI 14": round(rsi, 1),
                        "RSI Status": "OVERBOUGHT" if rsi >= 70 else "OVERSOLD" if rsi <= 30 else "BULLISH" if rsi >= 50 else "BEARISH",
                        "MACD": round(float(macd_line.iloc[-1]), 4),
                        "MACD Signal": round(float(macd_sig.iloc[-1]), 4),
                        "MACD Cross": "BULLISH CROSS" if up_macd else "BEARISH CROSS" if dn_macd else "NONE",
                        "VWAP": round(vwap_last, 2),
                        "Price vs VWAP": vwap_position,
                        "VWAP Gap %": round(vwap_gap_pct, 2),
                        "VWAP Distance Warning": "FAR ABOVE VWAP" if vwap_gap_pct >= 1.0 else "FAR BELOW VWAP" if vwap_gap_pct <= -1.0 else "NEAR VWAP",
                        "Fair Value Gap": fvg_type,
                        "FVG Low": round(float(fvg_low), 2) if pd.notna(fvg_low) else np.nan,
                        "FVG High": round(float(fvg_high), 2) if pd.notna(fvg_high) else np.nan,
                        "FVG Distance %": round(float(fvg_distance_pct), 2) if pd.notna(fvg_distance_pct) else np.nan,
                        "Price vs FVG": fvg_relation,
                        "FVG Age (bars)": int(fvg_bars_ago) if pd.notna(fvg_bars_ago) else np.nan,
                        "RVOL": round(rvol, 2),
                        "Indicator Summary": " | ".join(signal_parts)
                    })
                except Exception as ind_exc:
                    ind_errors.append(f"{ind_symbol}: {type(ind_exc).__name__}")
                ind_bar.progress(ind_i / max(len(ind_universe), 1),
                                 text=f"Indicator scan: {ind_i} / {len(ind_universe)}")
            ind_bar.empty()
            ind_result = pd.DataFrame(ind_rows)
            if not ind_result.empty:
                ind_result["_sort"] = ind_result["RVOL"].fillna(0)
                ind_result = ind_result.sort_values("_sort", ascending=False).drop(columns="_sort").reset_index(drop=True)
            st.session_state["additional_indicators_df"] = ind_result
            st.session_state["additional_indicators_errors"] = ind_errors
            st.session_state["additional_indicators_scanned_at"] = _now_ist().strftime("%Y-%m-%d %H:%M:%S IST")

        ind_result = st.session_state.get("additional_indicators_df", pd.DataFrame())
        if isinstance(ind_result, pd.DataFrame) and not ind_result.empty:
            st.caption(f"Last scan: {st.session_state.get('additional_indicators_scanned_at', 'N/A')} | Results: {len(ind_result)}")
            cross_filter = st.selectbox("Show results", ["All results", "Fresh EMA/MACD crosses", "Bullish EMA trend", "Bearish EMA trend", "Price above VWAP", "Price below VWAP", "FVG detected", "Far from VWAP (≥ 1%)", "Volume spike (RVOL ≥ 1.5)"], key="additional_ind_filter")
            ind_view = ind_result.copy()
            if cross_filter == "Fresh EMA/MACD crosses":
                ind_view = ind_view[
                    ind_view["EMA 9/21 Cross"].ne("NONE") |
                    ind_view["50/200 Cross"].ne("NONE") |
                    ind_view["MACD Cross"].ne("NONE")
                ]
            elif cross_filter == "Bullish EMA trend":
                ind_view = ind_view[ind_view["EMA Trend"] == "BULLISH"]
            elif cross_filter == "Bearish EMA trend":
                ind_view = ind_view[ind_view["EMA Trend"] == "BEARISH"]
            elif cross_filter == "Price above VWAP":
                ind_view = ind_view[ind_view["Price vs VWAP"] == "ABOVE"]
            elif cross_filter == "Price below VWAP":
                ind_view = ind_view[ind_view["Price vs VWAP"] == "BELOW"]
            elif cross_filter == "FVG detected":
                ind_view = ind_view[ind_view["Fair Value Gap"] != "NONE"]
            elif cross_filter == "Far from VWAP (≥ 1%)":
                ind_view = ind_view[ind_view["VWAP Gap %"].abs() >= 1.0]
            elif cross_filter == "Volume spike (RVOL ≥ 1.5)":
                ind_view = ind_view[ind_view["RVOL"] >= 1.5]
            st.dataframe(ind_view, use_container_width=True, height=520, hide_index=True)
            dc1, dc2 = st.columns(2)
            with dc1:
                st.download_button("📊 Download Excel", _format_excel_output(ind_view, "ADDITIONAL_INDICATORS"),
                                   f"ADDITIONAL_INDICATORS_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx",
                                   "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="additional_indicators_xlsx")
            with dc2:
                st.download_button("📄 Download CSV", to_csv_bytes(ind_view),
                                   f"ADDITIONAL_INDICATORS_{_now_ist().strftime('%Y%m%d_%H%M')}.csv",
                                   "text/csv", key="additional_indicators_csv")
        else:
            st.info("Click RUN ADDITIONAL INDICATORS. Start with 30–50 symbols to reduce FYERS API load.")
        ind_errors = st.session_state.get("additional_indicators_errors", [])
        if ind_errors:
            with st.expander(f"⚠️ Candle/API issues ({len(ind_errors)})"):
                st.write("\\n".join(ind_errors[:100]))

    # ════════════════════════════════════════════════════════════════════════════════
    # TAB 19: 20-MINUTE RANGE CONSOLIDATION → BIG MOVE
    # ════════════════════════════════════════════════════════════════════════════════
    with tabs[19]:
        st.markdown("### ⏱️ 20-MINUTE RANGE → BIG MOVE UP / DOWN")
        st.caption("Uses 1-minute candles. Only completed 1-minute candles from the latest session are used; the previous 20 define the range and the latest completed candle is checked separately. Repeat scans briefly use a 45-second cache.")
        range_universe_choice = st.radio(
            "Scan universe", ["NSE ALL + F&O (combined)", "NSE ALL only", "F&O only"],
            horizontal=True, key="range20_universe"
        )
        if range_universe_choice == "NSE ALL only":
            range_symbols = list(dict.fromkeys(all_symbols))
        elif range_universe_choice == "F&O only":
            range_symbols = list(dict.fromkeys(fo_symbols))
        else:
            range_symbols = list(dict.fromkeys(list(all_symbols) + list(fo_symbols)))
        st.caption(f"Selected symbols: {len(range_symbols)} | 1-minute candle history requests may take time for the full universe.")
        if st.button(f"🚀 SCAN 20M RANGE ({len(range_symbols)} symbols)", key="range20_run", type="primary"):
            with st.spinner("Scanning 1-minute candles for 20-minute consolidation and volume pressure…"):
                rr, re, rs = run_20m_range_scan(fyers, range_symbols)
                st.session_state["range20_df"] = pd.DataFrame(rr) if rr else pd.DataFrame()
                st.session_state["range20_errors"] = re
                st.session_state["range20_stats"] = rs
                st.session_state["range20_scanned_at"] = _now_ist().strftime("%d-%b-%Y %H:%M:%S IST")
        r20 = st.session_state.get("range20_df", pd.DataFrame())
        if isinstance(r20, pd.DataFrame) and not r20.empty:
            st.caption(f"Last scan: {st.session_state.get('range20_scanned_at', 'N/A')} | Rows: {len(r20)}")
            f1, f2, f3 = st.columns(3)
            with f1:
                status_filter = st.selectbox("20M Signal", ["ALL", "BIG MOVE UP WATCH", "BIG MOVE DOWN WATCH", "CONFIRMED BREAKOUT/BREAKDOWN", "RANGE + VOLUME BUILDUP — WAIT"], key="range20_status_filter")
            with f2:
                min_score = st.slider("Minimum pre-move score", 0, 100, 35, 5, key="range20_score_filter")
            with f3:
                min_buildup = st.slider("Minimum volume buildup (x)", 0.0, 3.0, 1.0, 0.1, key="range20_vol_filter")
            rview = r20.copy()
            if status_filter == "BIG MOVE UP WATCH":
                rview = rview[rview["20M STATUS"] == "🟢 BIG MOVE UP WATCH"]
            elif status_filter == "BIG MOVE DOWN WATCH":
                rview = rview[rview["20M STATUS"] == "🔴 BIG MOVE DOWN WATCH"]
            elif status_filter == "CONFIRMED BREAKOUT/BREAKDOWN":
                rview = rview[rview["20M BREAKOUT STATUS"].astype(str).str.startswith("CONFIRMED")]
            elif status_filter == "RANGE + VOLUME BUILDUP — WAIT":
                rview = rview[rview["20M STATUS"] == "🟡 RANGE + VOLUME BUILDUP — WAIT"]
            rview["20M PRE-MOVE SCORE"] = pd.to_numeric(rview["20M PRE-MOVE SCORE"], errors="coerce").fillna(0)
            rview["20M VOLUME BUILDUP X"] = pd.to_numeric(rview["20M VOLUME BUILDUP X"], errors="coerce").fillna(0)
            rview = rview[(rview["20M PRE-MOVE SCORE"] >= min_score) & (rview["20M VOLUME BUILDUP X"] >= min_buildup)]
            cols = ["SYMBOL", "LTP", "20M CANDLE TIME", "20M STATUS", "20M DIRECTION", "20M PRE-MOVE SCORE", "20M RANGE %", "20M RANGE HIGH", "20M RANGE LOW", "20M VOLUME BUILDUP X", "20M BUY PRESSURE %", "20M SELL PRESSURE %", "20M DIST TO HIGH %", "20M DIST TO LOW %", "20M BREAKOUT STATUS", "20M REASON"]
            rview = rview[[c for c in cols if c in rview.columns]]
            if rview.empty:
                st.info("No rows match these filters. Choose ALL or lower the score/volume threshold.")
            else:
                st.dataframe(rview, use_container_width=True, height=560, hide_index=True)
                d1, d2 = st.columns(2)
                with d1:
                    st.download_button("📊 Download Excel", _format_excel_output(rview, "20M_RANGE_BIG_MOVE"), f"20M_RANGE_BIG_MOVE_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="range20_xlsx")
                with d2:
                    st.download_button("📄 Download CSV", to_csv_bytes(rview), f"20M_RANGE_BIG_MOVE_{_now_ist().strftime('%Y%m%d_%H%M')}.csv", "text/csv", key="range20_csv")
        else:
            st.info("Click SCAN 20M RANGE to run this independent scan. Existing scanner tabs and signals are unchanged.")
        range_errors = st.session_state.get("range20_errors", [])
        if range_errors:
            with st.expander(f"⚠️ 1M candle/API issues ({len(range_errors)})"):
                st.write("\n".join(range_errors[:100]))
        st.caption("WATCH = early setup, not confirmed. CONFIRMED = latest completed 1M candle closed outside the previous 20-candle range and volume/pressure filters passed. Pressure is inferred from candle bodies and volume, not true exchange order-book depth.")


    # ════════════════════════════════════════════════════════════════════════════════
    # TAB 20: PRE-BIG MOVEMENT STOCK FINDER
    # Ranks existing 20M scan results; does not issue trades or alter old scanners.
    # ════════════════════════════════════════════════════════════════════════════════
    with tabs[20]:
        st.markdown("### 🎯 PRE-BIG MOVEMENT STOCK FINDER")
        st.caption(
            "Ranks stocks from the latest 20M range scan. First run the 20M RANGE BIG MOVE tab. "
            "WATCH means a developing setup; it is not a guaranteed prediction."
        )
        finder_df = st.session_state.get("range20_df", pd.DataFrame())
        if not isinstance(finder_df, pd.DataFrame) or finder_df.empty:
            st.info("ముందుగా ⏱️ 20M RANGE BIG MOVE tab లో scan run చేయండి. ఆ results ఇక్కడ rank అవుతాయి.")
        else:
            fv = finder_df.copy()
            for col in ["20M PRE-MOVE SCORE", "20M VOLUME BUILDUP X",
                        "20M BUY PRESSURE %", "20M SELL PRESSURE %",
                        "20M DIST TO HIGH %", "20M DIST TO LOW %"]:
                if col in fv.columns:
                    fv[col] = pd.to_numeric(fv[col], errors="coerce")

            c1, c2, c3 = st.columns(3)
            with c1:
                finder_direction = st.selectbox(
                    "Direction", ["BOTH", "BUY ONLY", "SELL ONLY"], key="prebig_finder_direction"
                )
            with c2:
                finder_min_score = st.slider(
                    "Minimum setup score", 0, 100, 45, 5, key="prebig_finder_score"
                )
            with c3:
                finder_top_n = st.selectbox(
                    "Show top stocks", [10, 20, 30, 50, 100], index=1, key="prebig_finder_topn"
                )

            if "20M DIRECTION" in fv.columns:
                if finder_direction == "BUY ONLY":
                    fv = fv[fv["20M DIRECTION"].astype(str).str.upper() == "BUY"]
                elif finder_direction == "SELL ONLY":
                    fv = fv[fv["20M DIRECTION"].astype(str).str.upper() == "SELL"]

            if "20M PRE-MOVE SCORE" in fv.columns:
                fv = fv[fv["20M PRE-MOVE SCORE"].fillna(0) >= finder_min_score]

            # Rank combines the existing setup score and volume buildup.
            # Do not fabricate delta/market-depth data when it is absent.
            score = pd.to_numeric(fv.get("20M PRE-MOVE SCORE", 0), errors="coerce")
            buildup = pd.to_numeric(fv.get("20M VOLUME BUILDUP X", 0), errors="coerce").fillna(0)
            fv["FINDER RANK SCORE"] = (
                score.fillna(0) * 0.80
                + (buildup.clip(0, 3) / 3.0 * 20.0)
            ).round(1)

            def _finder_label(row):
                status = str(row.get("20M STATUS", ""))
                direction = str(row.get("20M DIRECTION", "NONE")).upper()
                confirmed = str(row.get("20M BREAKOUT STATUS", "")).startswith("CONFIRMED")
                if confirmed and direction == "BUY":
                    return "🔥 BIG BUY CONFIRMED"
                if confirmed and direction == "SELL":
                    return "🔥 BIG SELL CONFIRMED"
                if direction == "BUY" and float(row.get("FINDER RANK SCORE", 0) or 0) >= 60:
                    return "🟢 PRE-BIG BUY WATCH"
                if direction == "SELL" and float(row.get("FINDER RANK SCORE", 0) or 0) >= 60:
                    return "🔴 PRE-BIG SELL WATCH"
                if "RANGE" in status.upper():
                    return "🟡 RANGE READY — WAIT"
                return "⚪ WAIT"

            fv["FINDER SIGNAL"] = fv.apply(_finder_label, axis=1)

            # High-priced, unusually active stocks: blink when LTP >= user threshold
            # and 20M volume buildup is above the selected multiple.
            if "LTP" in fv.columns:
                fv["LTP"] = pd.to_numeric(fv["LTP"], errors="coerce")
            else:
                fv["LTP"] = pd.NA
            fv["20M VOLUME BUILDUP X"] = pd.to_numeric(
                fv.get("20M VOLUME BUILDUP X", 0), errors="coerce"
            ).fillna(0)

            blink_c1, blink_c2 = st.columns(2)
            with blink_c1:
                blink_price_min = st.number_input(
                    "Blink stocks when price is above (₹)",
                    min_value=1.0, value=1000.0, step=100.0,
                    key="prebig_blink_price_min"
                )
            with blink_c2:
                blink_volume_min = st.number_input(
                    "Big volume threshold (x average volume)",
                    min_value=1.0, max_value=20.0, value=2.0, step=0.5,
                    key="prebig_blink_volume_min"
                )

            blink_df = fv[
                (fv["LTP"] >= blink_price_min)
                & (fv["20M VOLUME BUILDUP X"] >= blink_volume_min)
            ].copy()

            st.markdown(
                """
                <style>
                @keyframes preBigBlink {
                    0%, 100% { opacity: 1; box-shadow: 0 0 5px rgba(255,180,0,.25); }
                    50% { opacity: .35; box-shadow: 0 0 18px rgba(255,180,0,.9); }
                }
                .prebig-blink-card {
                    animation: preBigBlink 1s infinite;
                    border: 2px solid #f59e0b;
                    border-radius: 10px;
                    padding: 10px 14px;
                    margin: 7px 0;
                    background: rgba(245,158,11,.10);
                    font-weight: 700;
                }
                .prebig-blink-sub { font-size: 12px; font-weight: 500; opacity: .85; }
                </style>
                """,
                unsafe_allow_html=True
            )
            st.markdown("#### 🚨 PRICE ₹1,000+ & BIG VOLUME — BLINK ALERT")
            if blink_df.empty:
                st.info(
                    f"ప్రస్తుతం ₹{blink_price_min:,.0f}+ price మరియు "
                    f"{blink_volume_min:.1f}x+ 20M volume buildup ఉన్న stock లేదు."
                )
            else:
                st.success(f"{len(blink_df)} stocks matched — alert list")
                for _, blink_row in blink_df.sort_values(
                    "20M VOLUME BUILDUP X", ascending=False
                ).head(30).iterrows():
                    blink_symbol = str(blink_row.get("SYMBOL", "UNKNOWN"))
                    blink_direction = str(blink_row.get("20M DIRECTION", "WAIT"))
                    blink_ltp = pd.to_numeric(blink_row.get("LTP"), errors="coerce")
                    blink_vol = float(blink_row.get("20M VOLUME BUILDUP X", 0) or 0)
                    blink_score = float(blink_row.get("FINDER RANK SCORE", 0) or 0)
                    blink_price_text = f"₹{blink_ltp:,.2f}" if pd.notna(blink_ltp) else "N/A"
                    st.markdown(
                        f'<div class="prebig-blink-card">🚨 {blink_symbol} '
                        f'| {blink_price_text} | {blink_direction} '
                        f'| VOLUME {blink_vol:.2f}x | SCORE {blink_score:.1f}'
                        f'<div class="prebig-blink-sub">Price threshold and volume buildup conditions matched. '
                        f'Watch for range breakout confirmation.</div></div>',
                        unsafe_allow_html=True
                    )

            fv = fv.sort_values(
                ["FINDER RANK SCORE", "20M VOLUME BUILDUP X"],
                ascending=[False, False]
            ).head(finder_top_n)

            if fv.empty:
                st.warning("ఈ filters కి సరిపోయే stocks లేవు. Minimum score తగ్గించి చూడండి.")
            else:
                buy_count = int(fv["FINDER SIGNAL"].astype(str).str.contains("BUY", na=False).sum())
                sell_count = int(fv["FINDER SIGNAL"].astype(str).str.contains("SELL", na=False).sum())
                confirmed_count = int(fv["FINDER SIGNAL"].astype(str).str.contains("CONFIRMED", na=False).sum())
                m1, m2, m3 = st.columns(3)
                m1.metric("BUY WATCH / CONFIRMED", buy_count)
                m2.metric("SELL WATCH / CONFIRMED", sell_count)
                m3.metric("CONFIRMED BREAKOUTS", confirmed_count)

                show_cols = [
                    "SYMBOL", "LTP", "FINDER SIGNAL", "FINDER RANK SCORE",
                    "20M PRE-MOVE SCORE", "20M DIRECTION", "20M RANGE %",
                    "20M RANGE HIGH", "20M RANGE LOW", "20M VOLUME BUILDUP X",
                    "20M BUY PRESSURE %", "20M SELL PRESSURE %",
                    "20M BREAKOUT STATUS", "20M REASON"
                ]
                show_cols = [c for c in show_cols if c in fv.columns]
                st.dataframe(fv[show_cols], use_container_width=True, height=560, hide_index=True)
                x1, x2 = st.columns(2)
                with x1:
                    st.download_button(
                        "📊 Download Finder Excel",
                        _format_excel_output(fv[show_cols], "PRE_BIG_STOCK_FINDER"),
                        f"PRE_BIG_STOCK_FINDER_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx",
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        key="prebig_finder_xlsx"
                    )
                with x2:
                    st.download_button(
                        "📄 Download Finder CSV",
                        to_csv_bytes(fv[show_cols]),
                        f"PRE_BIG_STOCK_FINDER_{_now_ist().strftime('%Y%m%d_%H%M')}.csv",
                        "text/csv",
                        key="prebig_finder_csv"
                    )
            st.caption(
                "Important: Current finder ranks the 20M range scan's candle/volume-derived evidence. "
                "It does not claim true aggressor-side trade delta or guaranteed future movement. "
                "Use the LIVE ORDER BOOK tab separately for FYERS bid/ask depth snapshot."
            )

    with tabs[21]:
        _show_pattern_scan_tab(fyers, all_symbols, fo_symbols, mode="structure")

    with tabs[22]:
        _show_pattern_scan_tab(fyers, all_symbols, fo_symbols, mode="candle")

    with tabs[23]:
        _show_early_big_move_tab(fyers, all_symbols, fo_symbols)

    gc.collect()

# ════════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ════════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    try:
        access_token = os.environ.get("FYERS_ACCESS_TOKEN")
        if not access_token:
            st.error("❌ FYERS_ACCESS_TOKEN not set in environment variables")
            st.info("Please set: export FYERS_ACCESS_TOKEN='your_token_here'")
            st.stop()
        
        try:
            # Prefer the current FYERS SDK; retain legacy fallback for existing deployments.
            try:
                from fyers_apiv3 import fyersModel
            except ImportError:
                from fyers_api import fyersModel
        except ImportError:
            st.error("❌ FYERS SDK not installed")
            st.code("pip install fyers-apiv3", language="bash")
            st.info("Legacy environments may still use: pip install fyers-api")
            st.stop()
        
        app_id = os.environ.get("FYERS_APP_ID", "DEMO")
        
        try:
            fyers = fyersModel.FyersModel(client_id=app_id, token=access_token, log_path="")
            logger.info("Fyers client initialized successfully")
            show_scanner(fyers)
        except Exception as init_error:
            st.error(f"❌ Failed to initialize Fyers API: {str(init_error)}")
            logger.error(f"Fyers initialization error: {init_error}", exc_info=True)
            
            with st.expander("Debug Information"):
                st.write(f"App ID: {app_id}")
                st.write(f"Token set: {bool(access_token)}")
                st.write(f"Error: {init_error}")
    
    except Exception as e:
        st.error(f"❌ Unexpected Error: {e}")
        logger.error(f"Unexpected error: {e}", exc_info=True)
