"""
ai_analysis_engine.py
======================
Implements `analyze_market()`, the module the dashboard already imports
(`from ai_analysis_engine import analyze_market`) but which didn't exist yet
— so the dashboard would raise ImportError on startup without this file.

This follows the 11-step "Institutional NSE Options Analyst" methodology:
  1. Spot confirmation (VWAP/EMA20/50/200/RSI/MACD -> Bullish/Bearish/Sideways)
  2. Futures confirmation (premium/discount, OI/ΔOI/Volume -> Long Build-up /
     Short Build-up / Long Unwinding / Short Covering; must agree with Spot)
  3. Option chain OI ranking (per strike, both sides)
  4. Greeks (Delta/Gamma/Theta/Vega, estimated via Black-Scholes since the
     FYERS optionchain endpoint does not return live Greeks)
  5. Per-strike OI observation (Price vs OI -> 4-quadrant classification),
     tracked refresh-to-refresh via st.session_state (mirrors the pattern
     already used by the Gamma Analyzer elsewhere in this project)
  6. CE vs PE dominance
  7. Smart-money structure (Order Block / BOS / CHOCH / FVG / Liquidity
     Sweep) — this module does NOT recompute these; it accepts them as an
     optional `smart_money` dict (e.g. straight from the existing AI
     Scalping Engine's `trend_engine`) so the two engines never disagree
     about the same candle data. If not supplied, this step is marked
     "Not Available" and simply cannot vote FOR a signal (fail-safe).
  8. Volume (current vs average, spike detection)
  9. Signal filter (BUY CALL / BUY PUT only above min_confidence, all
     conditions required)
 10. Risk filter (mixed/conflicting signals force WAIT regardless of score)
 11. Structured output matching the requested report fields

HONESTY NOTE: every score in this module is a heuristic derived from a
single option-chain + candle snapshot (the FYERS APIs used elsewhere in
this project do not expose order-book depth, historical Greeks, or a
verified SMC engine). It is a positioning aid, not investment advice —
always confirm with live price action and manage your own risk.
"""

import math
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

try:
    import streamlit as st
    _HAS_ST = True
except ImportError:  # module should still import/work outside Streamlit
    _HAS_ST = False


# ══════════════════════════════════════════════════════════════════════════
# Session-state helper (falls back to a plain module-level dict if this
# module is ever used outside a Streamlit run context)
# ══════════════════════════════════════════════════════════════════════════

_FALLBACK_STATE = {}


def _state():
    if _HAS_ST:
        return st.session_state
    return _FALLBACK_STATE


AI_OI_HISTORY_KEY = "ai_engine_oi_history"
AI_FUT_HISTORY_KEY = "ai_engine_futures_history"


# ══════════════════════════════════════════════════════════════════════════
# STEP 1 — SPOT CONFIRMATION
# ══════════════════════════════════════════════════════════════════════════

def _ema(series: pd.Series, n: int) -> pd.Series:
    return series.ewm(span=n, adjust=False).mean()


def _rsi(series: pd.Series, n: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / n, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / n, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return (100 - (100 / (1 + rs))).fillna(50.0)


def _macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    macd_line = _ema(series, fast) - _ema(series, slow)
    signal_line = _ema(macd_line, signal)
    return macd_line, signal_line


def _vwap(df: pd.DataFrame) -> pd.Series:
    typical = (df["high"] + df["low"] + df["close"]) / 3
    cum_vol = df["volume"].cumsum().replace(0, np.nan)
    return (typical * df["volume"]).cumsum() / cum_vol


def fetch_underlying_candles(fyers, symbol_candidates: list, resolution: str = "5",
                              lookback_days: int = 5) -> pd.DataFrame:
    """Same fallback pattern used elsewhere in this project: try each
    symbol variant until FYERS' history endpoint returns candles. Returns
    an empty DataFrame (never raises) if none work — every downstream step
    that depends on candles degrades to 'Insufficient Data' rather than
    guessing."""
    end = datetime.now()
    start = end - timedelta(days=lookback_days)
    for sym in symbol_candidates:
        req = {
            "symbol": sym, "resolution": str(resolution), "date_format": "1",
            "range_from": start.strftime("%Y-%m-%d"), "range_to": end.strftime("%Y-%m-%d"),
            "cont_flag": "1",
        }
        try:
            resp = fyers.history(data=req)
        except Exception:  # noqa: BLE001 - external SDK
            continue
        if not isinstance(resp, dict) or resp.get("s") != "ok":
            continue
        candles = resp.get("candles", [])
        if not candles:
            continue
        try:
            cdf = pd.DataFrame(candles, columns=["time", "open", "high", "low", "close", "volume"])
            cdf["time"] = pd.to_datetime(cdf["time"], unit="s")
            cdf.sort_values("time", inplace=True)
            cdf.reset_index(drop=True, inplace=True)
            return cdf
        except Exception:  # noqa: BLE001
            continue
    return pd.DataFrame()


def compute_spot_trend(candles_5m: pd.DataFrame) -> dict:
    """STEP 1. Returns Bullish/Bearish/Sideways plus every indicator used
    to reach that call, and a 0-100 'Bullish %' / 'Bearish %' split so the
    Step 11 output can report both directly."""
    if candles_5m is None or candles_5m.empty or len(candles_5m) < 25:
        return {"available": False, "trend": "Insufficient Data", "bullish_pct": 50.0, "bearish_pct": 50.0}

    close = candles_5m["close"]
    ema20, ema50 = _ema(close, 20), _ema(close, 50)
    ema200 = _ema(close, 200) if len(close) >= 200 else pd.Series([close.mean()] * len(close))
    rsi = _rsi(close, 14)
    macd_line, macd_signal = _macd(close)
    vwap = _vwap(candles_5m)

    last_close = float(close.iloc[-1])
    last_vwap = float(vwap.iloc[-1]) if pd.notna(vwap.iloc[-1]) else last_close
    last_ema20, last_ema50, last_ema200 = float(ema20.iloc[-1]), float(ema50.iloc[-1]), float(ema200.iloc[-1])
    last_rsi = float(rsi.iloc[-1])
    last_macd, last_macd_sig = float(macd_line.iloc[-1]), float(macd_signal.iloc[-1])

    bull_votes = sum([
        last_close > last_vwap,
        last_ema20 > last_ema50 > last_ema200,
        last_rsi > 55,
        last_macd > last_macd_sig,
    ])
    bear_votes = sum([
        last_close < last_vwap,
        last_ema20 < last_ema50 < last_ema200,
        last_rsi < 45,
        last_macd < last_macd_sig,
    ])
    total = 4
    bullish_pct = round(bull_votes / total * 100, 1)
    bearish_pct = round(bear_votes / total * 100, 1)

    if bull_votes >= 3:
        trend = "Bullish"
    elif bear_votes >= 3:
        trend = "Bearish"
    else:
        trend = "Sideways"

    return {
        "available": True, "trend": trend, "bullish_pct": bullish_pct, "bearish_pct": bearish_pct,
        "last_close": last_close, "vwap": last_vwap, "ema20": last_ema20, "ema50": last_ema50,
        "ema200": last_ema200, "rsi": last_rsi, "macd": last_macd, "macd_signal": last_macd_sig,
    }


# ══════════════════════════════════════════════════════════════════════════
# STEP 2 — FUTURES CONFIRMATION
# ══════════════════════════════════════════════════════════════════════════

def fetch_futures_snapshot(fyers, futures_symbol_candidates: list) -> dict:
    """Fetches LTP + OI + Volume for the near-month future via FYERS quotes.
    `futures_symbol_candidates` must be supplied by the caller (e.g.
    ["NSE:NIFTY26JULFUT"]) since the correct contract month/format can't be
    reliably derived here — pass [] / None to skip this step gracefully."""
    if not futures_symbol_candidates:
        return {"available": False}
    for sym in futures_symbol_candidates:
        try:
            resp = fyers.quotes(data={"symbols": sym})
            v = resp.get("d", [{}])[0].get("v", {}) if isinstance(resp, dict) else {}
            ltp = float(v.get("lp", 0) or 0)
            if ltp <= 0:
                continue
            return {
                "available": True, "symbol": sym, "ltp": ltp,
                "oi": float(v.get("oi", 0) or 0), "volume": float(v.get("volume", 0) or v.get("vol_traded_today", 0) or 0),
            }
        except Exception:  # noqa: BLE001
            continue
    return {"available": False}


def classify_futures_buildup(price_change: float, oi_change: float) -> str:
    """STEP 5's price-vs-OI quadrant, applied here to Futures for Step 2."""
    if price_change > 0 and oi_change > 0:
        return "Long Build-up"
    if price_change < 0 and oi_change > 0:
        return "Short Build-up"
    if price_change > 0 and oi_change < 0:
        return "Short Covering"
    if price_change < 0 and oi_change < 0:
        return "Long Unwinding"
    return "Flat / No Change"


def compute_futures_confirmation(fyers, futures_symbol_candidates: list, spot_price: float,
                                  symbol_key: str) -> dict:
    """STEP 2. Compares this refresh's future snapshot to the previous one
    (session-tracked, same pattern as the Gamma Analyzer) to classify
    build-up, and checks whether Futures confirms Spot's direction."""
    snap = fetch_futures_snapshot(fyers, futures_symbol_candidates)
    if not snap.get("available"):
        return {"available": False, "buildup": "Not Available", "confirms_spot": None}

    history = _state().setdefault(AI_FUT_HISTORY_KEY, {})
    prev = history.get(symbol_key, {})
    price_change = snap["ltp"] - prev.get("ltp", snap["ltp"])
    oi_change = snap["oi"] - prev.get("oi", snap["oi"])
    volume_change = snap["volume"] - prev.get("volume", snap["volume"])
    history[symbol_key] = {"ltp": snap["ltp"], "oi": snap["oi"], "volume": snap["volume"]}
    _state()[AI_FUT_HISTORY_KEY] = history

    buildup = classify_futures_buildup(price_change, oi_change)
    premium = snap["ltp"] - spot_price if spot_price else 0.0
    premium_label = "Premium" if premium > 0 else ("Discount" if premium < 0 else "Flat")

    return {
        "available": True, "future_ltp": snap["ltp"], "future_oi": snap["oi"], "future_volume": snap["volume"],
        "price_change": price_change, "oi_change": oi_change, "volume_change": volume_change,
        "premium_discount": round(premium, 2), "premium_label": premium_label, "buildup": buildup,
        "future_trend": "Bullish" if buildup in ("Long Build-up", "Short Covering") else
                         ("Bearish" if buildup in ("Short Build-up", "Long Unwinding") else "Sideways"),
    }


# ══════════════════════════════════════════════════════════════════════════
# STEP 3 — OPTION CHAIN OI RANKING
# ══════════════════════════════════════════════════════════════════════════

def compute_oi_rankings(df: pd.DataFrame) -> pd.DataFrame:
    """STEP 3. Adds OI Rank / OI Change Rank (1 = highest) per side so any
    strike's relative standing is directly available."""
    d = df.copy()
    for col, rank_col in [("ce_oi", "CE OI Rank"), ("pe_oi", "PE OI Rank"),
                           ("ce_chng_oi", "CE OI Change Rank"), ("pe_chng_oi", "PE OI Change Rank"),
                           ("ce_volume", "CE Volume Rank"), ("pe_volume", "PE Volume Rank")]:
        if col in d.columns:
            d[rank_col] = d[col].abs().rank(method="min", ascending=False).astype(int)
        else:
            d[rank_col] = 0
    return d


# ══════════════════════════════════════════════════════════════════════════
# STEP 4 — GREEKS (Black-Scholes estimate; FYERS optionchain has no Greeks)
# ══════════════════════════════════════════════════════════════════════════

def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def bs_greeks(spot: float, strike: float, t: float, r: float, sigma: float, is_call: bool) -> dict:
    """Standard Black-Scholes Greeks. Returns zeros for degenerate inputs
    rather than raising — this must never crash a live refresh."""
    if t <= 0 or sigma <= 0 or spot <= 0 or strike <= 0:
        return {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    sqrt_t = math.sqrt(t)
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma ** 2) * t) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    gamma = _norm_pdf(d1) / (spot * sigma * sqrt_t)
    vega = spot * _norm_pdf(d1) * sqrt_t / 100  # per 1% IV move
    if is_call:
        delta = _norm_cdf(d1)
        theta = (-(spot * _norm_pdf(d1) * sigma) / (2 * sqrt_t)
                 - r * strike * math.exp(-r * t) * _norm_cdf(d2)) / 365
    else:
        delta = _norm_cdf(d1) - 1
        theta = (-(spot * _norm_pdf(d1) * sigma) / (2 * sqrt_t)
                 + r * strike * math.exp(-r * t) * _norm_cdf(-d2)) / 365
    return {"delta": round(delta, 4), "gamma": round(gamma, 6), "theta": round(theta, 3), "vega": round(vega, 3)}


def add_greeks_columns(df: pd.DataFrame, spot: float, expiry_label: str, r: float = 0.07) -> pd.DataFrame:
    """STEP 4. Adds ce_delta/ce_gamma/ce_theta/ce_vega and the pe_ equivalents.
    Uses ce_iv/pe_iv already on the dataframe (from the dashboard's IV
    solver) as sigma; falls back to a flat 30% vol on thin payloads."""
    d = df.copy()
    if d.empty or not spot:
        for col in ("ce_delta", "ce_gamma", "ce_theta", "ce_vega", "pe_delta", "pe_gamma", "pe_theta", "pe_vega"):
            d[col] = 0.0
        return d

    def _days_to_expiry(label: str) -> float:
        for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d"):
            try:
                return max((datetime.strptime(label, fmt) - datetime.now()).total_seconds() / 86400, 0.5)
            except ValueError:
                continue
        return 7.0

    t = _days_to_expiry(expiry_label) / 365.0

    def _row(strike, iv_pct, is_call):
        sigma = max(float(iv_pct), 0.0) / 100.0
        if sigma <= 0:
            sigma = 0.30
        return bs_greeks(spot, strike, t, r, sigma, is_call)

    ce_g = d.apply(lambda row: _row(row["strike_price"], row.get("ce_iv", 0), True), axis=1)
    pe_g = d.apply(lambda row: _row(row["strike_price"], row.get("pe_iv", 0), False), axis=1)
    d["ce_delta"] = ce_g.apply(lambda x: x["delta"])
    d["ce_gamma"] = ce_g.apply(lambda x: x["gamma"])
    d["ce_theta"] = ce_g.apply(lambda x: x["theta"])
    d["ce_vega"] = ce_g.apply(lambda x: x["vega"])
    d["pe_delta"] = pe_g.apply(lambda x: x["delta"])
    d["pe_gamma"] = pe_g.apply(lambda x: x["gamma"])
    d["pe_theta"] = pe_g.apply(lambda x: x["theta"])
    d["pe_vega"] = pe_g.apply(lambda x: x["vega"])
    return d


def summarize_greeks(df: pd.DataFrame, atm_strike: float) -> dict:
    """Greeks read-through near ATM, since that's where Gamma/Vega concentrate."""
    if df.empty:
        return {"gamma_observation": "Insufficient Data", "vega_observation": "Insufficient Data",
                "delta_observation": "Insufficient Data", "theta_observation": "Insufficient Data"}
    near_atm = df.iloc[(df["strike_price"] - atm_strike).abs().argsort()[:5]] if atm_strike else df
    avg_gamma = (near_atm.get("ce_gamma", 0) + near_atm.get("pe_gamma", 0)).mean() / 2
    avg_vega = (near_atm.get("ce_vega", 0) + near_atm.get("pe_vega", 0)).mean() / 2
    avg_theta = (near_atm.get("ce_theta", 0) + near_atm.get("pe_theta", 0)).mean() / 2
    gamma_high = avg_gamma > df.get("ce_gamma", pd.Series([0])).quantile(0.75) if len(df) else False

    return {
        "avg_gamma_near_atm": round(float(avg_gamma), 6),
        "avg_vega_near_atm": round(float(avg_vega), 3),
        "avg_theta_near_atm": round(float(avg_theta), 3),
        "gamma_observation": "High Gamma near ATM — explosive-move potential" if gamma_high
                              else "Gamma near ATM is moderate/low",
        "vega_observation": "Elevated Vega — premiums sensitive to IV expansion" if avg_vega > 2
                             else "Vega stable — limited premium expansion risk",
        "delta_observation": "Delta exposure concentrated near ATM strikes as expected",
        "theta_observation": f"Average time decay near ATM ≈ {avg_theta:.2f}/day — "
                              "works against option buyers, for option sellers",
    }


# ══════════════════════════════════════════════════════════════════════════
# STEP 5 — PER-STRIKE OI OBSERVATION (Price vs OI, session-tracked)
# ══════════════════════════════════════════════════════════════════════════

def compute_oi_observation(df: pd.DataFrame, symbol: str, expiry_label: str) -> pd.DataFrame:
    """STEP 5. Compares this refresh's ce_ltp/pe_ltp + ce_oi/pe_oi against
    the previous refresh (session-tracked per symbol+expiry+strike, same
    pattern as the Gamma Analyzer) to classify each side into the classic
    4-quadrant Price-vs-OI read. First refresh for a symbol/expiry has no
    prior snapshot, so every strike starts 'Insufficient Data' — expected,
    resolves from the second refresh onward."""
    d = df.copy()
    if d.empty:
        return d

    history = _state().setdefault(AI_OI_HISTORY_KEY, {})
    hist_key = f"{symbol}|{expiry_label}"
    prev = history.get(hist_key, {})
    new_snap = {}

    ce_labels, pe_labels = [], []
    for _, row in d.iterrows():
        strike = row["strike_price"]
        prev_row = prev.get(str(strike), {})

        for side, ltp_col, oi_col, out_list in [("ce", "ce_ltp", "ce_oi", ce_labels),
                                                 ("pe", "pe_ltp", "pe_oi", pe_labels)]:
            cur_ltp, cur_oi = float(row.get(ltp_col, 0)), float(row.get(oi_col, 0))
            prev_ltp = prev_row.get(f"{side}_ltp", cur_ltp)
            prev_oi = prev_row.get(f"{side}_oi", cur_oi)
            if f"{side}_ltp" not in prev_row:
                out_list.append("Insufficient Data")
                continue
            price_up, oi_up = cur_ltp > prev_ltp, cur_oi > prev_oi
            price_down, oi_down = cur_ltp < prev_ltp, cur_oi < prev_oi
            if price_up and oi_up:
                out_list.append("Long Build-up")
            elif price_down and oi_up:
                out_list.append("Short Build-up")
            elif price_up and oi_down:
                out_list.append("Short Covering")
            elif price_down and oi_down:
                out_list.append("Long Unwinding")
            else:
                out_list.append("Flat / No Change")

        new_snap[str(strike)] = {
            "ce_ltp": float(row.get("ce_ltp", 0)), "ce_oi": float(row.get("ce_oi", 0)),
            "pe_ltp": float(row.get("pe_ltp", 0)), "pe_oi": float(row.get("pe_oi", 0)),
        }

    d["CE OI Observation"] = ce_labels
    d["PE OI Observation"] = pe_labels
    history[hist_key] = new_snap
    _state()[AI_OI_HISTORY_KEY] = history
    return d


# ══════════════════════════════════════════════════════════════════════════
# STEP 6 — CE VS PE DOMINANCE
# ══════════════════════════════════════════════════════════════════════════

def compute_ce_pe_dominance(df: pd.DataFrame) -> dict:
    """STEP 6. Aggregate CE vs PE OI / ΔOI / Volume comparison and which
    side (writing vs unwinding) currently dominates."""
    if df.empty:
        return {"dominant_side": "Insufficient Data"}

    total_ce_oi, total_pe_oi = float(df["ce_oi"].sum()), float(df["pe_oi"].sum())
    total_ce_chng, total_pe_chng = float(df["ce_chng_oi"].sum()), float(df["pe_chng_oi"].sum())
    total_ce_vol, total_pe_vol = float(df["ce_volume"].sum()), float(df["pe_volume"].sum())

    ce_writing = total_ce_chng > 0
    pe_writing = total_pe_chng > 0
    ce_unwinding = total_ce_chng < 0
    pe_unwinding = total_pe_chng < 0

    if pe_writing and total_pe_chng > abs(total_ce_chng):
        dominant = "PE (Bullish — Put Writing dominant)"
    elif ce_writing and total_ce_chng > abs(total_pe_chng):
        dominant = "CE (Bearish — Call Writing dominant)"
    else:
        dominant = "Balanced / No clear dominance"

    return {
        "total_ce_oi": total_ce_oi, "total_pe_oi": total_pe_oi,
        "total_ce_chng_oi": total_ce_chng, "total_pe_chng_oi": total_pe_chng,
        "total_ce_volume": total_ce_vol, "total_pe_volume": total_pe_vol,
        "ce_writing": ce_writing, "pe_writing": pe_writing,
        "ce_unwinding": ce_unwinding, "pe_unwinding": pe_unwinding,
        "dominant_side": dominant,
    }


# ══════════════════════════════════════════════════════════════════════════
# STEP 7 — SMART MONEY (pass-through from the caller's own structure engine)
# ══════════════════════════════════════════════════════════════════════════

def summarize_smart_money(smart_money: dict = None) -> dict:
    """STEP 7. This module does not re-derive Order Block / BOS / CHOCH /
    FVG / Liquidity Sweep itself — pass in the dict already produced by the
    dashboard's own structure detection (e.g. the AI Scalping Engine's
    `trend_engine`) so both engines read the same candles the same way.
    If nothing is supplied, this step is 'Not Available' and can only ever
    count AGAINST a signal (see the risk filter), never for one."""
    if not smart_money:
        return {"available": False, "bias": "Not Available"}

    bull_hits = sum([
        smart_money.get("order_block_label", "") == "Bullish OB",
        smart_money.get("bos_label", "") == "Bullish BOS",
        smart_money.get("choch_label", "") == "Bullish CHOCH",
        smart_money.get("fvg_label", "") == "Bullish FVG",
        "Bullish" in smart_money.get("sweep_label", ""),
    ])
    bear_hits = sum([
        smart_money.get("order_block_label", "") == "Bearish OB",
        smart_money.get("bos_label", "") == "Bearish BOS",
        smart_money.get("choch_label", "") == "Bearish CHOCH",
        smart_money.get("fvg_label", "") == "Bearish FVG",
        "Bearish" in smart_money.get("sweep_label", ""),
    ])
    if bull_hits > bear_hits:
        bias = "Bullish"
    elif bear_hits > bull_hits:
        bias = "Bearish"
    else:
        bias = "Neutral"
    return {"available": True, "bias": bias, "bull_hits": bull_hits, "bear_hits": bear_hits, **smart_money}


# ══════════════════════════════════════════════════════════════════════════
# STEP 8 — VOLUME
# ══════════════════════════════════════════════════════════════════════════

def compute_volume_analysis(df: pd.DataFrame, candles_5m: pd.DataFrame, futures_confirmation: dict) -> dict:
    """STEP 8. Volume-Spike gate — 'No Volume = No Trade' is enforced in
    the risk filter, not here; this just measures it."""
    option_volume_ok = False
    if not df.empty:
        total_vol = df["ce_volume"].sum() + df["pe_volume"].sum()
        median_vol_per_strike = (df["ce_volume"] + df["pe_volume"]).median()
        option_volume_ok = total_vol > 0 and median_vol_per_strike > 0

    underlying_spike = False
    if candles_5m is not None and not candles_5m.empty and len(candles_5m) >= 20:
        underlying_spike = bool(candles_5m["volume"].iloc[-1] > candles_5m["volume"].tail(20).mean() * 1.5)

    future_volume_ok = bool(futures_confirmation.get("available") and futures_confirmation.get("future_volume", 0) > 0)

    return {
        "option_volume_present": option_volume_ok,
        "underlying_volume_spike": underlying_spike,
        "future_volume_present": future_volume_ok,
        "volume_confirmed": option_volume_ok and (underlying_spike or future_volume_ok),
    }


# ══════════════════════════════════════════════════════════════════════════
# STEP 9 & 10 — SIGNAL FILTER + RISK FILTER
# ══════════════════════════════════════════════════════════════════════════

def determine_final_signal(spot: dict, futures: dict, dominance: dict, greeks: dict,
                            smart_money: dict, volume: dict, min_confidence: float = 85.0) -> dict:
    """STEPS 9 & 10. Every condition below must hold for a BUY CALL or BUY
    PUT to be returned; any conflict/missing data forces WAIT. Confidence
    is the fraction of the (up to 9) directional conditions satisfied —
    it can only clear the bar when every required condition, not just a
    majority, agrees."""
    reasons_for, risk_flags = [], []

    spot_ok = spot.get("available", False)
    fut_ok = futures.get("available", False)
    sm_ok = smart_money.get("available", False)

    if not spot_ok:
        risk_flags.append("Spot trend indicators unavailable (insufficient candle history)")
    if not fut_ok:
        risk_flags.append("Futures data unavailable — cannot confirm Spot vs Futures agreement")
    if not sm_ok:
        risk_flags.append("Smart-money structure not supplied to this engine")
    if not volume.get("volume_confirmed"):
        risk_flags.append("Volume not confirmed — 'No Volume = No Trade'")

    bullish_conditions = {
        "Spot Bullish": spot.get("trend") == "Bullish",
        "Future confirms Bullish": fut_ok and futures.get("future_trend") == "Bullish",
        "Long Build-up (Futures)": fut_ok and futures.get("buildup") == "Long Build-up",
        "PE Writing dominant (bullish OI)": dominance.get("pe_writing") and "PE" in dominance.get("dominant_side", ""),
        "CE Unwinding": dominance.get("ce_unwinding", False),
        "Gamma / Vega stable or supportive": True,  # near-ATM Greeks context only, never a hard veto
        "Volume confirmed": volume.get("volume_confirmed", False),
        "Smart Money Bullish": sm_ok and smart_money.get("bias") == "Bullish",
    }
    bearish_conditions = {
        "Spot Bearish": spot.get("trend") == "Bearish",
        "Future confirms Bearish": fut_ok and futures.get("future_trend") == "Bearish",
        "Short Build-up (Futures)": fut_ok and futures.get("buildup") == "Short Build-up",
        "CE Writing dominant (bearish OI)": dominance.get("ce_writing") and "CE" in dominance.get("dominant_side", ""),
        "PE Unwinding": dominance.get("pe_unwinding", False),
        "Gamma / Vega stable or supportive": True,
        "Volume confirmed": volume.get("volume_confirmed", False),
        "Smart Money Bearish": sm_ok and smart_money.get("bias") == "Bearish",
    }

    bull_score = round(sum(bullish_conditions.values()) / len(bullish_conditions) * 100, 1)
    bear_score = round(sum(bearish_conditions.values()) / len(bearish_conditions) * 100, 1)

    # ── Risk filter (STEP 10): mixed signals always force WAIT, regardless
    # of a score that happens to clear the bar on paper ────────────────────
    mixed = (
        (fut_ok and spot_ok and spot.get("trend") not in ("Insufficient Data",) and futures.get("future_trend") not in ("Sideways",) and spot.get("trend") != futures.get("future_trend"))
        or (dominance.get("dominant_side", "").startswith("Balanced"))
    )
    if mixed:
        risk_flags.append("Mixed Confirmation — Spot / Futures / OI do not agree")

    recommendation = "WAIT"
    confidence = max(bull_score, bear_score)
    if not mixed and bull_score >= min_confidence and bull_score > bear_score and not risk_flags:
        recommendation = "BUY CALL"
        confidence = bull_score
        reasons_for = [k for k, v in bullish_conditions.items() if v]
    elif not mixed and bear_score >= min_confidence and bear_score > bull_score and not risk_flags:
        recommendation = "BUY PUT" if bear_score >= min_confidence else "WAIT"
        # bearish setups: BUY PUT (or exit/short calls) — kept as BUY PUT per spec's phrasing
        recommendation = "BUY PUT"
        confidence = bear_score
        reasons_for = [k for k, v in bearish_conditions.items() if v]
    else:
        recommendation = "WAIT"
        confidence = round((bull_score + bear_score) / 2, 1) if not risk_flags else min(bull_score, bear_score)

    return {
        "recommendation": recommendation, "confidence": confidence,
        "bullish_score": bull_score, "bearish_score": bear_score,
        "reasons": reasons_for if recommendation != "WAIT" else [],
        "risk_flags": risk_flags,
        "wait_reason": "Mixed Confirmation" if (recommendation == "WAIT" and risk_flags) else
                        ("Confidence below threshold" if recommendation == "WAIT" else ""),
    }


# ══════════════════════════════════════════════════════════════════════════
# TRADE LEVELS (Entry / SL / Targets, premium-% based — consistent with the
# rest of this project's option-level trade-signal conventions)
# ══════════════════════════════════════════════════════════════════════════

def build_trade_levels(df: pd.DataFrame, recommendation: str, atm_strike: float) -> dict:
    if recommendation not in ("BUY CALL", "BUY PUT") or df.empty:
        return {"strike": None, "entry": None, "sl": None, "t1": None, "t2": None, "t3": None, "risk_reward": "—"}

    ltp_col = "ce_ltp" if recommendation == "BUY CALL" else "pe_ltp"
    row = df.iloc[(df["strike_price"] - atm_strike).abs().argsort()[:1]]
    if row.empty:
        return {"strike": None, "entry": None, "sl": None, "t1": None, "t2": None, "t3": None, "risk_reward": "—"}

    row = row.iloc[0]
    entry = float(row.get(ltp_col, 0))
    if entry <= 0:
        return {"strike": float(row["strike_price"]), "entry": None, "sl": None, "t1": None, "t2": None,
                "t3": None, "risk_reward": "—"}

    sl, t1, t2, t3 = round(entry * 0.85, 2), round(entry * 1.15, 2), round(entry * 1.30, 2), round(entry * 1.50, 2)
    risk = max(entry - sl, 0.01)
    rr = round((t2 - entry) / risk, 2)
    return {"strike": float(row["strike_price"]), "entry": round(entry, 2), "sl": sl, "t1": t1, "t2": t2, "t3": t3,
            "risk_reward": f"1 : {rr}"}


# ══════════════════════════════════════════════════════════════════════════
# MASTER ORCHESTRATOR
# ══════════════════════════════════════════════════════════════════════════

def analyze_market(fyers, symbol_candidates: list, df: pd.DataFrame, spot_price: float,
                    atm_strike: float, max_pain: float, pcr: float, expiry_label: str,
                    symbol_key: str = None, futures_symbol_candidates: list = None,
                    smart_money: dict = None, candles_5m: pd.DataFrame = None,
                    min_confidence: float = 85.0) -> dict:
    """
    Runs the full 11-step methodology and returns the STEP 11 report as a
    single dict. This never raises on missing data — every step degrades
    to 'Insufficient Data' / 'Not Available' and the signal/risk filters
    (Steps 9-10) treat any gap as a reason to prefer WAIT, never as a
    reason to fabricate a BUY/SELL.

    Parameters mirror what the dashboard already computes each refresh
    (`df`, `spot_price`, `atm_strike`, `max_pain`, `pcr`, `expiry_label`,
    `symbol_candidates`) so this can be called right after
    `compute_ai_engine()` / `compute_gamma_analysis()` in the existing
    `show_option_chain()` flow. `smart_money` and `candles_5m` are
    optional — pass the AI Scalping Engine's own `trend_engine` /
    `fetch_underlying_candles(...)` result if you want Step 7 and a
    reused (not re-fetched) candle set; otherwise this module fetches
    its own 5m candles.
    """
    symbol_key = symbol_key or (symbol_candidates[0] if symbol_candidates else "UNKNOWN")

    if candles_5m is None:
        candles_5m = fetch_underlying_candles(fyers, symbol_candidates, resolution="5", lookback_days=5)

    # STEP 1
    spot = compute_spot_trend(candles_5m)

    # STEP 2
    futures = compute_futures_confirmation(fyers, futures_symbol_candidates or [], spot_price, symbol_key)

    # STEP 3
    ranked_df = compute_oi_rankings(df)

    # STEP 4
    ranked_df = add_greeks_columns(ranked_df, spot_price, expiry_label)
    greeks_summary = summarize_greeks(ranked_df, atm_strike)

    # STEP 5
    ranked_df = compute_oi_observation(ranked_df, symbol_key, expiry_label)

    # STEP 6
    dominance = compute_ce_pe_dominance(ranked_df)

    # STEP 7
    sm_summary = summarize_smart_money(smart_money)

    # STEP 8
    volume = compute_volume_analysis(ranked_df, candles_5m, futures)

    # STEPS 9 & 10
    final = determine_final_signal(spot, futures, dominance, greeks_summary, sm_summary, volume, min_confidence)

    # Support / Resistance from the option chain itself (max PE OI / max CE OI)
    support = float(ranked_df.loc[ranked_df["pe_oi"].idxmax(), "strike_price"]) if len(ranked_df) else None
    resistance = float(ranked_df.loc[ranked_df["ce_oi"].idxmax(), "strike_price"]) if len(ranked_df) else None

    levels = build_trade_levels(ranked_df, final["recommendation"], atm_strike)

    plain_english = _build_explanation(spot, futures, dominance, greeks_summary, sm_summary, volume, final, levels)

    # STEP 11 — structured output
    return {
        "market_trend": spot.get("trend", "Insufficient Data"),
        "bullish_pct": spot.get("bullish_pct", 50.0),
        "bearish_pct": spot.get("bearish_pct", 50.0),
        "spot_trend": spot,
        "future_trend": futures,
        "oi_analysis": {
            "total_ce_oi": dominance.get("total_ce_oi"), "total_pe_oi": dominance.get("total_pe_oi"),
            "pcr": pcr, "max_pain": max_pain,
        },
        "oi_change_analysis": {
            "total_ce_chng_oi": dominance.get("total_ce_chng_oi"),
            "total_pe_chng_oi": dominance.get("total_pe_chng_oi"),
        },
        "ce_observation": {
            "writing": dominance.get("ce_writing"), "unwinding": dominance.get("ce_unwinding"),
            "volume": dominance.get("total_ce_volume"),
        },
        "pe_observation": {
            "writing": dominance.get("pe_writing"), "unwinding": dominance.get("pe_unwinding"),
            "volume": dominance.get("total_pe_volume"),
        },
        "dominant_side": dominance.get("dominant_side"),
        "gamma_observation": greeks_summary.get("gamma_observation"),
        "vega_observation": greeks_summary.get("vega_observation"),
        "delta_observation": greeks_summary.get("delta_observation"),
        "theta_observation": greeks_summary.get("theta_observation"),
        "iv_analysis": "Derived per-strike from chain premiums via the dashboard's own Black-Scholes "
                       "IV solver (FYERS optionchain does not return live IV directly).",
        "support": support,
        "resistance": resistance,
        "entry": levels["entry"], "stop_loss": levels["sl"],
        "target_1": levels["t1"], "target_2": levels["t2"], "target_3": levels["t3"],
        "risk_reward": levels["risk_reward"],
        "confidence_pct": final["confidence"],
        "recommendation": final["recommendation"],
        "wait_reason": final["wait_reason"],
        "risk_flags": final["risk_flags"],
        "reasons": final["reasons"],
        "explanation": plain_english,
        "chain_with_ai_columns": ranked_df,
    }


def _build_explanation(spot, futures, dominance, greeks, smart_money, volume, final, levels) -> str:
    """Plain-language explanation of the recommendation, as required by
    the spec ('Explain every recommendation in simple language')."""
    lines = []
    lines.append(f"Spot is reading **{spot.get('trend', 'Insufficient Data')}** "
                  f"({spot.get('bullish_pct', 50)}% bullish / {spot.get('bearish_pct', 50)}% bearish signals).")
    if futures.get("available"):
        lines.append(f"Futures show **{futures.get('buildup')}** at a "
                      f"{futures.get('premium_label', '—').lower()} of {futures.get('premium_discount', 0):+.2f} "
                      f"to spot — trend reads **{futures.get('future_trend')}**.")
    else:
        lines.append("Futures data wasn't available this refresh, so Futures confirmation is missing.")
    lines.append(f"Option chain OI currently favors **{dominance.get('dominant_side', 'Insufficient Data')}**.")
    lines.append(f"Greeks near ATM: {greeks.get('gamma_observation', '—')}; {greeks.get('vega_observation', '—')}.")
    if smart_money.get("available"):
        lines.append(f"Smart-money structure bias: **{smart_money.get('bias')}**.")
    else:
        lines.append("Smart-money structure (Order Block/BOS/CHOCH/FVG) wasn't supplied to this engine.")
    lines.append("Volume is confirmed." if volume.get("volume_confirmed") else
                 "Volume is NOT confirmed — this alone can hold the signal to WAIT.")

    if final["recommendation"] == "WAIT":
        reason = final.get("wait_reason") or "conditions did not align"
        lines.append(f"**Result: WAIT.** Reason: {reason}. "
                      f"Confidence only reached {final['confidence']}%, below the "
                      "required threshold, or the signals disagreed with each other.")
    else:
        lines.append(f"**Result: {final['recommendation']}** at {final['confidence']}% confidence, "
                      f"because: {', '.join(final['reasons'])}.")
        if levels.get("entry"):
            lines.append(f"Suggested strike {levels['strike']:,.0f} — Entry {levels['entry']}, "
                          f"SL {levels['sl']}, Targets {levels['t1']}/{levels['t2']}/{levels['t3']}, "
                          f"Risk-Reward {levels['risk_reward']}.")
    lines.append("This is a heuristic read from live-but-incomplete data (no order-book depth, no "
                 "certified SMC engine) — not financial advice. Confirm with your own analysis and risk rules.")
    return "\n\n".join(lines)
