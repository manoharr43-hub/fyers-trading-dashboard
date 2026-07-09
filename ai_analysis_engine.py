"""
ai_analysis_engine.py
======================
Implements analyze_market(), the module the dashboard imports as:

    from ai_analysis_engine import analyze_market

INSTITUTIONAL-GRADE DECISION FRAMEWORK
---------------------------------------
Every call walks through a fixed, non-skippable ten-layer thinking order
before a single BUY/SELL signal is allowed to exist. A layer that has no
data available degrades to NEUTRAL and can only ever push the engine
toward caution (WAIT) — it can never manufacture confirmation:

    1.  Global Market            (macro_context["global_market_trend"])
    2.  India VIX                (macro_context["india_vix"], ["india_vix_trend"])
    3.  SGX / GIFT Nifty         (macro_context["giftnifty_change_pct"])
    4.  US Market                (macro_context["us_market_trend"])
    5.  Sector Strength          (macro_context["sector_strength"])
    6.  Index Trend              (trend_engine: EMA20/50/200, Supertrend, ADX)
    7.  Smart Money              (OI+ΔOI proxy, institutional flow if given)
    8.  Dealer Positioning       (Max Pain, Call/Put Wall, Zero-Gamma, Pin Risk)
    9.  Option Sellers           (writing pressure, theta/IV edge)
    10. Option Buyers            (fresh buying, momentum, structure)

Only after all ten layers have been read does the engine evaluate the
strict, ALL-conditions-true CE BUY / PE BUY / CE SELL / PE SELL
checklists, weight them, and — if and only if every gate holds — emit a
signal. Missing data, disagreement between Smart Money / Dealer /
Seller bias and the candidate trade, or a Reward:Risk below 1:2 all
fail safe to WAIT. Capital protection is the highest priority in this
file; nothing here is allowed to manufacture a signal out of missing
information.

INPUT CONTRACT (unchanged)
---------------------------
analyze_market() still runs AFTER the dashboard's own pipeline has
computed its per-strike columns:

    df = compute_big_move_scores(df, spot_price, max_pain, pcr, atm_strike)
    df = compute_ai_engine(df, spot_price, atm_strike, max_pain, pcr)

Every existing positional/keyword argument, function name, and return
key is unchanged. This upgrade only ADDS optional keyword-only inputs
(oi_history, macro_context) with default None — existing call sites
keep working exactly as before, just without the extra layers turned
on. The public return dict gains a few additive keys
("decision_layers", "signal_engine_meta") alongside every key that
existed previously; nothing that existed before was removed or
renamed.

This is a heuristic, snapshot-based read-through of the option chain
and (optionally) recent underlying candles, historical OI snapshots,
and macro context. It is not financial advice, it does not have
access to real order-flow/dealer-position data, and several
"Smart Money" / "Dealer" / "Gamma" concepts below are necessarily
proxied from OI/Volume/ΔOI/Gamma rather than measured directly.
Always confirm with live price action and manage your own risk.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

# ══════════════════════════════════════════════════════════════════════════
# Tunable thresholds
# ══════════════════════════════════════════════════════════════════════════

MIN_PROBABILITY = 85.0
MIN_CONFIDENCE = 85.0
MIN_RISK_REWARD = 2.0  # capital protection floor — reject anything below 1:2

# Percentile-based "heavy" thresholds — relative to the current chain
# snapshot, since only a single point-in-time payload is guaranteed.
HEAVY_OI_PCT = 0.80
HEAVY_CHNG_OI_PCT = 0.80
HIGH_VOLUME_PCT = 0.75
IV_HIGH_PCT = 0.75
IV_LOW_PCT = 0.25
PREMIUM_VOLUME_SPIKE_MULT = 2.0

RISK_LEVELS = ["Low", "Moderate", "Elevated", "High"]

# Weighted-probability category weights (STEP-9 scoring — sums to 1.0)
CATEGORY_WEIGHTS = {
    "trend": 0.20,
    "oi": 0.20,
    "price_action": 0.15,
    "volume": 0.10,
    "greeks": 0.10,
    "pcr": 0.05,
    "iv": 0.05,
    "dealer": 0.05,
    "institutional": 0.05,
    "smart_money": 0.05,
}
assert abs(sum(CATEGORY_WEIGHTS.values()) - 1.0) < 1e-9

DECISION_LAYER_ORDER = [
    "global_market", "india_vix", "sgx_giftnifty", "us_market", "sector_strength",
    "index_trend", "smart_money", "dealer_positioning", "option_sellers", "option_buyers",
]


# ══════════════════════════════════════════════════════════════════════════
# Small helpers
# ══════════════════════════════════════════════════════════════════════════

def _safe_quantile(series: pd.Series, q: float) -> float:
    s = pd.to_numeric(series, errors="coerce").dropna()
    if s.empty:
        return 0.0
    return float(s.quantile(q))


def _pct_rank(value: float, series: pd.Series) -> float:
    """0-1 percentile rank of `value` within `series`."""
    s = pd.to_numeric(series, errors="coerce").dropna()
    if s.empty or s.max() == s.min():
        return 0.5
    return float((s < value).mean())


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _safe_float(v, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        f = float(v)
        return f if not math.isnan(f) else default
    except (TypeError, ValueError):
        return default


def _oi_price_action_signal(price_chg_pct: Optional[float], oi_chg_pct: Optional[float]) -> str:
    """Classic OI + Price interpretation matrix:
        Price up   + OI up   -> Long Build-up   (fresh buying)
        Price up   + OI down -> Short Covering
        Price down + OI up   -> Short Build-up  (fresh writing/selling)
        Price down + OI down -> Long Unwinding
    Returns "Unavailable" if either input is missing."""
    if price_chg_pct is None or oi_chg_pct is None:
        return "Unavailable"
    if price_chg_pct >= 0 and oi_chg_pct >= 0:
        return "Long Build-up"
    if price_chg_pct >= 0 and oi_chg_pct < 0:
        return "Short Covering"
    if price_chg_pct < 0 and oi_chg_pct >= 0:
        return "Short Build-up"
    return "Long Unwinding"


def _snapshot_delta(history: Optional[dict], key: str, current: float) -> Optional[float]:
    """Pull a prior value for `key` out of an oi_history bucket
    (shape: {"5m": {...}, "15m": {...}, "30m": {...}, "1hr": {...}}) and
    return the % change vs `current`. Returns None if unavailable."""
    if not history:
        return None
    prev = history.get(key)
    if prev is None or prev == 0:
        return None
    try:
        return (current - prev) / abs(prev) * 100.0
    except ZeroDivisionError:
        return None


# ══════════════════════════════════════════════════════════════════════════
# Bias primitives (STEP 1-10, "Final Output Format" bias fields)
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class MarketContext:
    """Chain-wide context computed once per analyze_market() call and
    shared by every per-strike evaluation, so every strike is judged
    against the same ten-layer read: Global Market -> India VIX ->
    SGX/GIFT Nifty -> US Market -> Sector Strength -> Index Trend ->
    Smart Money -> Dealer Positioning -> Option Sellers -> Option Buyers."""

    spot_price: float
    atm_strike: float
    max_pain: float
    pcr: float
    support: Optional[float]
    resistance: Optional[float]
    expiry_label: str = ""

    # Chain-wide thresholds, filled in by build_market_context()
    heavy_ce_oi_thresh: float = 0.0
    heavy_pe_oi_thresh: float = 0.0
    heavy_ce_chng_thresh: float = 0.0
    heavy_pe_chng_thresh: float = 0.0
    high_ce_vol_thresh: float = 0.0
    high_pe_vol_thresh: float = 0.0
    ce_iv_high_thresh: float = 0.0
    ce_iv_low_thresh: float = 0.0
    pe_iv_high_thresh: float = 0.0
    pe_iv_low_thresh: float = 0.0
    avg_ce_iv: float = 0.0
    avg_pe_iv: float = 0.0

    # Trend-engine derived (may all be None if trend_engine unavailable)
    price_above_vwap: Optional[bool] = None
    price_below_vwap: Optional[bool] = None
    vwap_rejection: Optional[bool] = None
    vwap_reclaim: Optional[bool] = None
    above_ema20: Optional[bool] = None
    above_ema50: Optional[bool] = None
    above_ema200: Optional[bool] = None
    below_ema20: Optional[bool] = None
    below_ema50: Optional[bool] = None
    below_ema200: Optional[bool] = None
    ema_aligned_bullish: Optional[bool] = None
    ema_aligned_bearish: Optional[bool] = None
    bullish_structure: Optional[bool] = None
    bearish_structure: Optional[bool] = None
    choch_bullish: Optional[bool] = None
    choch_bearish: Optional[bool] = None
    fvg_bullish: Optional[bool] = None
    fvg_bearish: Optional[bool] = None
    volume_expansion: Optional[bool] = None
    adx_strong: Optional[bool] = None
    supertrend_dir: Optional[int] = None  # 1 bullish, -1 bearish
    atr: Optional[float] = None

    # Price-action pattern layer
    open_eq_high: Optional[bool] = None
    open_eq_low: Optional[bool] = None
    fake_breakout: Optional[bool] = None
    fake_breakdown: Optional[bool] = None
    liquidity_sweep_high: Optional[bool] = None
    liquidity_sweep_low: Optional[bool] = None
    stop_hunt_detected: Optional[bool] = None
    buyer_trap: Optional[bool] = None
    seller_trap: Optional[bool] = None

    # Greeks / gamma layer
    delta_acceleration: Optional[bool] = None
    gamma_acceleration: Optional[bool] = None
    gamma_flip_bullish: Optional[bool] = None
    gamma_flip_bearish: Optional[bool] = None
    zero_gamma_strike: Optional[float] = None
    call_wall: Optional[float] = None
    put_wall: Optional[float] = None
    pin_risk: bool = False

    # IV regime layer
    iv_rank_ce: Optional[float] = None
    iv_rank_pe: Optional[float] = None
    iv_percentile_ce: Optional[float] = None
    iv_percentile_pe: Optional[float] = None
    iv_expansion: Optional[bool] = None
    iv_crush: Optional[bool] = None

    # Max Pain shift (intraday)
    max_pain_prev: Optional[float] = None
    max_pain_shift_bias: str = "⚪ Unavailable"

    # OI-shift-over-time layer (5m/15m/30m/1hr)
    oi_shift_available: bool = False
    index_oi_signal: str = "Unavailable"

    # Macro layers 1-5
    global_market_bias: str = "⚪ Unavailable"
    india_vix_bias: str = "⚪ Unavailable"
    india_vix_level: Optional[float] = None
    india_vix_trend: Optional[str] = None
    sgx_giftnifty_bias: str = "⚪ Unavailable"
    us_market_bias: str = "⚪ Unavailable"
    sector_strength_bias: str = "⚪ Unavailable"
    banknifty_nifty_corr_bias: str = "⚪ Unavailable"
    macro_available: bool = False
    macro_contradicts_bulls: bool = False
    macro_contradicts_bears: bool = False

    # Expiry-rules layer
    expiry_type: Optional[str] = None       # "weekly" | "monthly" | None
    is_expiry_day: bool = False

    # Bias summary (final output)
    market_bias: str = "🟡 Neutral"
    smart_money_bias: str = "🟡 Neutral"
    seller_bias: str = "🟡 Balanced"
    buyer_bias: str = "🟡 Weak"
    institutional_bias: str = "🟡 Neutral"
    dealer_bias: str = "🟡 Neutral"
    pcr_bias: str = "🟡 Neutral"
    oi_bias: str = "🟡 Neutral"
    gamma_bias: str = "🟡 Neutral"
    iv_bias: str = "🟡 Neutral"
    index_trend_bias: str = "🟡 Neutral"
    expected_direction: str = "🟡 Range-bound"

    has_trend_engine: bool = False
    has_gamma: bool = False


def _layer_1_global_market(macro: Optional[dict]) -> str:
    if not macro or macro.get("global_market_trend") is None:
        return "⚪ Unavailable"
    trend = str(macro.get("global_market_trend", "")).lower()
    if trend in ("up", "bullish", "positive"):
        return "🟢 Global markets supportive"
    if trend in ("down", "bearish", "negative"):
        return "🔴 Global markets weak"
    return "🟡 Global markets mixed"


def _layer_2_india_vix(macro: Optional[dict]) -> tuple[str, Optional[float], Optional[str]]:
    if not macro or macro.get("india_vix") is None:
        return "⚪ Unavailable", None, None
    vix = _safe_float(macro.get("india_vix"))
    vix_trend = str(macro.get("india_vix_trend", "")).lower() or None
    if vix >= 20 or vix_trend == "rising_fast":
        bias = "🔴 VIX elevated/rising — hedging & option-selling favored, buyer caution"
    elif vix <= 12 and vix_trend in (None, "falling", "flat"):
        bias = "🟢 VIX low/falling — risk-on, directional buying favored"
    else:
        bias = "🟡 VIX neutral"
    return bias, vix, vix_trend


def _layer_3_sgx_giftnifty(macro: Optional[dict]) -> str:
    if not macro or macro.get("giftnifty_change_pct") is None:
        return "⚪ Unavailable"
    chg = _safe_float(macro.get("giftnifty_change_pct"))
    if chg > 0.15:
        return "🟢 GIFT Nifty pointing to a gap-up"
    if chg < -0.15:
        return "🔴 GIFT Nifty pointing to a gap-down"
    return "🟡 GIFT Nifty flat"


def _layer_4_us_market(macro: Optional[dict]) -> str:
    if not macro or macro.get("us_market_trend") is None:
        return "⚪ Unavailable"
    trend = str(macro.get("us_market_trend", "")).lower()
    if trend in ("up", "bullish", "positive"):
        return "🟢 US markets closed higher"
    if trend in ("down", "bearish", "negative"):
        return "🔴 US markets closed lower"
    return "🟡 US markets mixed"


def _layer_5_sector_strength(macro: Optional[dict]) -> tuple[str, str]:
    if not macro or macro.get("sector_strength") is None:
        return "⚪ Unavailable", "⚪ Unavailable"
    sector = str(macro.get("sector_strength", "")).lower()
    if sector in ("leading", "strong", "outperforming"):
        sector_bias = "🟢 Sector rotation into strength"
    elif sector in ("lagging", "weak", "underperforming"):
        sector_bias = "🔴 Sector rotation out of strength"
    else:
        sector_bias = "🟡 Sector rotation neutral"
    corr = macro.get("banknifty_nifty_corr")
    if corr is None:
        corr_bias = "⚪ Unavailable"
    else:
        corr = _safe_float(corr)
        corr_bias = "🟢 BankNifty/Nifty correlated" if corr >= 0.7 else "🟡 Correlation weakening"
    return sector_bias, corr_bias


def build_market_context(df: pd.DataFrame, spot_price: float, atm_strike: float,
                          max_pain: float, pcr: float, support: Optional[float],
                          resistance: Optional[float], trend_engine: Optional[dict] = None,
                          expiry_label: str = "", oi_history: Optional[dict] = None,
                          macro_context: Optional[dict] = None) -> MarketContext:
    """Runs the full ten-layer read BEFORE any per-strike BUY/SELL
    condition is evaluated:

        1 Global Market -> 2 India VIX -> 3 SGX/GIFT Nifty -> 4 US Market
        -> 5 Sector Strength -> 6 Index Trend -> 7 Smart Money
        -> 8 Dealer Positioning -> 9 Option Sellers -> 10 Option Buyers

    `oi_history` and `macro_context` are optional and additive — every
    call site that only ever passed the original arguments keeps
    working exactly as before, just with those extra layers reporting
    "⚪ Unavailable" and therefore never able to force a BUY/SELL."""
    ctx = MarketContext(
        spot_price=spot_price, atm_strike=atm_strike, max_pain=max_pain, pcr=pcr,
        support=support, resistance=resistance, expiry_label=expiry_label,
    )
    if df is None or df.empty:
        return ctx

    ce_oi = df.get("ce_oi", pd.Series(dtype=float))
    pe_oi = df.get("pe_oi", pd.Series(dtype=float))
    ce_chng = df.get("ce_chng_oi", pd.Series(dtype=float))
    pe_chng = df.get("pe_chng_oi", pd.Series(dtype=float))
    ce_vol = df.get("ce_volume", pd.Series(dtype=float))
    pe_vol = df.get("pe_volume", pd.Series(dtype=float))
    ce_iv = df.get("ce_iv", pd.Series(dtype=float))
    pe_iv = df.get("pe_iv", pd.Series(dtype=float))

    ctx.heavy_ce_oi_thresh = _safe_quantile(ce_oi, HEAVY_OI_PCT)
    ctx.heavy_pe_oi_thresh = _safe_quantile(pe_oi, HEAVY_OI_PCT)
    ctx.heavy_ce_chng_thresh = _safe_quantile(ce_chng.clip(lower=0), HEAVY_CHNG_OI_PCT)
    ctx.heavy_pe_chng_thresh = _safe_quantile(pe_chng.clip(lower=0), HEAVY_CHNG_OI_PCT)
    ctx.high_ce_vol_thresh = _safe_quantile(ce_vol, HIGH_VOLUME_PCT)
    ctx.high_pe_vol_thresh = _safe_quantile(pe_vol, HIGH_VOLUME_PCT)
    ctx.ce_iv_high_thresh = _safe_quantile(ce_iv, IV_HIGH_PCT)
    ctx.ce_iv_low_thresh = _safe_quantile(ce_iv, IV_LOW_PCT)
    ctx.pe_iv_high_thresh = _safe_quantile(pe_iv, IV_HIGH_PCT)
    ctx.pe_iv_low_thresh = _safe_quantile(pe_iv, IV_LOW_PCT)
    ctx.avg_ce_iv = float(ce_iv[ce_iv > 0].mean()) if (ce_iv > 0).any() else 0.0
    ctx.avg_pe_iv = float(pe_iv[pe_iv > 0].mean()) if (pe_iv > 0).any() else 0.0
    ctx.has_gamma = "gamma" in df.columns

    # ══════════════════════════════════════════════════════════════
    # LAYER 1-5 — MACRO (Global Market, VIX, SGX/GIFT Nifty, US, Sector)
    # ══════════════════════════════════════════════════════════════
    ctx.global_market_bias = _layer_1_global_market(macro_context)
    ctx.india_vix_bias, ctx.india_vix_level, ctx.india_vix_trend = _layer_2_india_vix(macro_context)
    ctx.sgx_giftnifty_bias = _layer_3_sgx_giftnifty(macro_context)
    ctx.us_market_bias = _layer_4_us_market(macro_context)
    ctx.sector_strength_bias, ctx.banknifty_nifty_corr_bias = _layer_5_sector_strength(macro_context)
    ctx.macro_available = any(b != "⚪ Unavailable" for b in
                              [ctx.global_market_bias, ctx.india_vix_bias, ctx.sgx_giftnifty_bias,
                               ctx.us_market_bias, ctx.sector_strength_bias])
    macro_biases = [ctx.global_market_bias, ctx.sgx_giftnifty_bias, ctx.us_market_bias, ctx.sector_strength_bias]
    ctx.macro_contradicts_bulls = sum(b.startswith("🔴") for b in macro_biases) >= 2
    ctx.macro_contradicts_bears = sum(b.startswith("🟢") for b in macro_biases) >= 2

    # Expiry-rules layer
    if macro_context:
        ctx.expiry_type = macro_context.get("expiry_type")
        ctx.is_expiry_day = bool(macro_context.get("is_expiry_day", False))

    # Institutional flow, if the caller has real order-flow data
    institutional_flow = (macro_context or {}).get("institutional_flow")  # "buying"/"selling"/None

    # ── PCR Bias ──────────────────────────────────────────────────────
    if pcr > 1.3:
        ctx.pcr_bias = "🟢 Bullish"
    elif pcr < 0.7:
        ctx.pcr_bias = "🔴 Bearish"
    else:
        ctx.pcr_bias = "🟡 Neutral"

    # ── OI Bias (which side is building more aggressively overall) ──────
    total_ce_chng_up = float(ce_chng.clip(lower=0).sum())
    total_pe_chng_up = float(pe_chng.clip(lower=0).sum())
    if total_pe_chng_up > total_ce_chng_up * 1.15:
        ctx.oi_bias = "🟢 Put Writers in Control (Bullish)"
    elif total_ce_chng_up > total_pe_chng_up * 1.15:
        ctx.oi_bias = "🔴 Call Writers in Control (Bearish)"
    else:
        ctx.oi_bias = "🟡 Balanced OI Build-up"

    # ── Historical OI shift (5m/15m/30m/1hr) — index/futures level ───────
    if oi_history:
        ctx.oi_shift_available = True
        idx_price_chg = oi_history.get("index_price_chg_pct")
        idx_oi_chg = oi_history.get("index_oi_chg_pct")
        ctx.index_oi_signal = _oi_price_action_signal(idx_price_chg, idx_oi_chg)
        ctx.max_pain_prev = oi_history.get("prev_max_pain")
        if ctx.max_pain_prev:
            shift = max_pain - ctx.max_pain_prev
            if abs(shift) < 1e-9:
                ctx.max_pain_shift_bias = "🟡 Max Pain stable"
            elif shift > 0:
                ctx.max_pain_shift_bias = "🟢 Max Pain shifting higher (bullish dealer hedging)"
            else:
                ctx.max_pain_shift_bias = "🔴 Max Pain shifting lower (bearish dealer hedging)"
        prev_avg_iv = oi_history.get("prev_avg_iv")
        cur_avg_iv = (ctx.avg_ce_iv + ctx.avg_pe_iv) / 2
        if prev_avg_iv:
            if cur_avg_iv >= prev_avg_iv * 1.10:
                ctx.iv_expansion, ctx.iv_crush = True, False
            elif cur_avg_iv <= prev_avg_iv * 0.90:
                ctx.iv_expansion, ctx.iv_crush = False, True
            else:
                ctx.iv_expansion, ctx.iv_crush = False, False
        iv_hist_ce = oi_history.get("iv_history_ce")  # list of past CE IV readings
        iv_hist_pe = oi_history.get("iv_history_pe")
        if iv_hist_ce:
            lo, hi = min(iv_hist_ce), max(iv_hist_ce)
            if hi > lo:
                ctx.iv_rank_ce = round(_clip01((ctx.avg_ce_iv - lo) / (hi - lo)) * 100, 1)
            ctx.iv_percentile_ce = round(_pct_rank(ctx.avg_ce_iv, pd.Series(iv_hist_ce)) * 100, 1)
        if iv_hist_pe:
            lo, hi = min(iv_hist_pe), max(iv_hist_pe)
            if hi > lo:
                ctx.iv_rank_pe = round(_clip01((ctx.avg_pe_iv - lo) / (hi - lo)) * 100, 1)
            ctx.iv_percentile_pe = round(_pct_rank(ctx.avg_pe_iv, pd.Series(iv_hist_pe)) * 100, 1)
        prev_avg_gamma = oi_history.get("prev_avg_gamma")
        if ctx.has_gamma and "Gamma Change" in df.columns and prev_avg_gamma is not None:
            cur_avg_gamma = float(df["Gamma Change"].mean())
            ctx.gamma_flip_bullish = prev_avg_gamma <= 0 < cur_avg_gamma
            ctx.gamma_flip_bearish = prev_avg_gamma >= 0 > cur_avg_gamma
            ctx.gamma_acceleration = abs(cur_avg_gamma) > abs(prev_avg_gamma) * 1.2

    # ── Dealer Positioning layer (STEP 8): Call Wall / Put Wall / Zero
    # Gamma / Pin Risk / Max Pain ──────────────────────────────────────
    if "ce_oi" in df.columns and not df["ce_oi"].dropna().empty:
        ctx.call_wall = float(df.loc[df["ce_oi"].idxmax(), "strike_price"])
    if "pe_oi" in df.columns and not df["pe_oi"].dropna().empty:
        ctx.put_wall = float(df.loc[df["pe_oi"].idxmax(), "strike_price"])
    if ctx.has_gamma:
        net_gamma = df.sort_values("strike_price")[["strike_price", "gamma"]].dropna()
        sign_changes = net_gamma["gamma"].apply(lambda g: 1 if g >= 0 else -1).diff().fillna(0)
        flips = net_gamma.loc[sign_changes != 0, "strike_price"]
        if not flips.empty:
            ctx.zero_gamma_strike = float(flips.iloc[(flips - spot_price).abs().argsort().iloc[0]])
    if max_pain and spot_price:
        ctx.pin_risk = abs(spot_price - max_pain) / max_pain * 100 < 0.3

    # ── Seller Bias (STEP 1 legacy naming, now the "Option Sellers"
    # layer #9) — sellers are assumed in control unless buyers are
    # clearly overwhelming them with fresh volume + ΔOI ─────────────────
    seller_dominant_ce = total_ce_chng_up >= ctx.heavy_ce_chng_thresh > 0
    seller_dominant_pe = total_pe_chng_up >= ctx.heavy_pe_chng_thresh > 0
    if seller_dominant_ce and not seller_dominant_pe:
        ctx.seller_bias = "🔴 Call Sellers Defending (Resistance)"
    elif seller_dominant_pe and not seller_dominant_ce:
        ctx.seller_bias = "🟢 Put Sellers Defending (Support)"
    elif seller_dominant_ce and seller_dominant_pe:
        ctx.seller_bias = "🟡 Sellers Active Both Sides (Range)"
    else:
        ctx.seller_bias = "🟡 Sellers Passive"

    # ── IV Bias ───────────────────────────────────────────────────────
    avg_iv = (ctx.avg_ce_iv + ctx.avg_pe_iv) / 2
    if avg_iv >= max(ctx.ce_iv_high_thresh, ctx.pe_iv_high_thresh) and avg_iv > 0:
        ctx.iv_bias = "🔴 Elevated IV (favor sellers / caution on buying)"
    elif 0 < avg_iv <= min(ctx.ce_iv_low_thresh or avg_iv, ctx.pe_iv_low_thresh or avg_iv):
        ctx.iv_bias = "🟢 Low/Compressed IV"
    else:
        ctx.iv_bias = "🟡 Neutral IV"

    # ── Gamma Bias ────────────────────────────────────────────────────
    if ctx.has_gamma and "Gamma Change" in df.columns:
        avg_gc = float(df["Gamma Change"].mean())
        if avg_gc > 0:
            ctx.gamma_bias = "🟢 Gamma Building (accelerating moves)"
        elif avg_gc < 0:
            ctx.gamma_bias = "🔴 Gamma Fading (favor sellers / pinning)"
        else:
            ctx.gamma_bias = "🟡 Gamma Flat"
    else:
        ctx.gamma_bias = "⚪ Gamma data unavailable"

    # ── Institutional / Smart Money / Dealer Bias (OI+ΔOI proxies,
    # layer #7 Smart Money + layer #8 Dealer Positioning) ────────────────
    high_oi_pe = df.loc[pe_oi >= ctx.heavy_pe_oi_thresh, "pe_chng_oi"].clip(lower=0).sum() if "pe_chng_oi" in df.columns else 0
    high_oi_ce = df.loc[ce_oi >= ctx.heavy_ce_oi_thresh, "ce_chng_oi"].clip(lower=0).sum() if "ce_chng_oi" in df.columns else 0
    if high_oi_pe > high_oi_ce * 1.15:
        ctx.institutional_bias = "🟢 Institutional Buying (Put support)"
        ctx.smart_money_bias = "🟢 Accumulation"
    elif high_oi_ce > high_oi_pe * 1.15:
        ctx.institutional_bias = "🔴 Institutional Selling (Call resistance)"
        ctx.smart_money_bias = "🔴 Distribution"
    else:
        ctx.institutional_bias = "🟡 Neutral"
        ctx.smart_money_bias = "🟡 Neutral"

    # Real institutional flow (if the caller supplies it) overrides the
    # OI proxy rather than being ignored — but only ever tightens, since
    # a disagreement between the two is itself useful caution.
    if institutional_flow == "buying" and ctx.institutional_bias.startswith("🔴"):
        ctx.institutional_bias = "🟡 Neutral (OI proxy vs flow disagree)"
    elif institutional_flow == "selling" and ctx.institutional_bias.startswith("🟢"):
        ctx.institutional_bias = "🟡 Neutral (OI proxy vs flow disagree)"

    # Dealer bias proxy: high total OI concentrated near ATM (pin risk) vs
    # spread out (directional room) — a rough Max-Pain-vs-Spot read.
    if max_pain and spot_price:
        dist_pct = abs(spot_price - max_pain) / max_pain * 100
        if dist_pct < 0.3:
            ctx.dealer_bias = "🟡 Pin Risk Near Max Pain"
        elif spot_price > max_pain:
            ctx.dealer_bias = "🟢 Spot Above Max Pain (dealers may hedge higher)"
        else:
            ctx.dealer_bias = "🔴 Spot Below Max Pain (dealers may hedge lower)"

    # ── Trend-engine derived checks (VWAP/EMA/Structure/Volume/ATR) ─────
    if trend_engine and trend_engine.get("available"):
        ctx.has_trend_engine = True
        last_close = trend_engine.get("last_close")
        prev_close = trend_engine.get("prev_close")
        vwap = trend_engine.get("vwap")
        ema20, ema50, ema200 = trend_engine.get("ema20"), trend_engine.get("ema50"), trend_engine.get("ema200")
        adx = trend_engine.get("adx", 0)
        buy_checks = trend_engine.get("buy_checks", {})
        sell_checks = trend_engine.get("sell_checks", {})
        day_open, day_high, day_low = trend_engine.get("day_open"), trend_engine.get("day_high"), trend_engine.get("day_low")
        ctx.atr = trend_engine.get("atr")

        if last_close is not None and vwap is not None:
            ctx.price_above_vwap = last_close > vwap
            ctx.price_below_vwap = last_close < vwap
            if prev_close is not None:
                ctx.vwap_rejection = prev_close > vwap and last_close < vwap
                ctx.vwap_reclaim = prev_close < vwap and last_close > vwap
        if last_close is not None and ema20 is not None:
            ctx.above_ema20 = last_close > ema20
            ctx.below_ema20 = last_close < ema20
        if last_close is not None and ema50 is not None:
            ctx.above_ema50 = last_close > ema50
            ctx.below_ema50 = last_close < ema50
        if last_close is not None and ema200 is not None:
            ctx.above_ema200 = last_close > ema200
            ctx.below_ema200 = last_close < ema200
        if None not in (ema20, ema50, ema200):
            ctx.ema_aligned_bullish = ema20 > ema50 > ema200
            ctx.ema_aligned_bearish = ema20 < ema50 < ema200

        ctx.bullish_structure = bool(buy_checks.get("Bullish BOS")) or bool(buy_checks.get("Bullish Order Block"))
        ctx.bearish_structure = bool(sell_checks.get("Bearish BOS")) or bool(sell_checks.get("Bearish Order Block"))
        ctx.choch_bullish = bool(buy_checks.get("Bullish CHoCH") or trend_engine.get("choch") == "bullish")
        ctx.choch_bearish = bool(sell_checks.get("Bearish CHoCH") or trend_engine.get("choch") == "bearish")
        ctx.fvg_bullish = bool(trend_engine.get("fvg_bullish"))
        ctx.fvg_bearish = bool(trend_engine.get("fvg_bearish"))
        ctx.volume_expansion = bool(buy_checks.get("Volume Spike") or sell_checks.get("Volume Spike"))
        ctx.adx_strong = adx > 25
        ctx.supertrend_dir = trend_engine.get("supertrend_dir")

        if day_open is not None and day_high is not None:
            ctx.open_eq_high = abs(day_open - day_high) / max(day_high, 1) < 0.0005
        if day_open is not None and day_low is not None:
            ctx.open_eq_low = abs(day_open - day_low) / max(day_low, 1) < 0.0005

        if day_high is not None and last_close is not None:
            swept_high = trend_engine.get("wick_above_high", False)
            ctx.liquidity_sweep_high = bool(swept_high) and last_close < day_high
            ctx.fake_breakout = bool(trend_engine.get("broke_high_reversed", False))
            ctx.buyer_trap = ctx.fake_breakout or (ctx.liquidity_sweep_high and bool(ctx.bearish_structure))
        if day_low is not None and last_close is not None:
            swept_low = trend_engine.get("wick_below_low", False)
            ctx.liquidity_sweep_low = bool(swept_low) and last_close > day_low
            ctx.fake_breakdown = bool(trend_engine.get("broke_low_reversed", False))
            ctx.seller_trap = ctx.fake_breakdown or (ctx.liquidity_sweep_low and bool(ctx.bullish_structure))
        ctx.stop_hunt_detected = bool(ctx.liquidity_sweep_high or ctx.liquidity_sweep_low)

        delta_now, delta_prev = trend_engine.get("delta"), trend_engine.get("prev_delta")
        if delta_now is not None and delta_prev is not None:
            ctx.delta_acceleration = abs(delta_now) > abs(delta_prev) * 1.15

    # ── Index Trend Bias (layer #6) — rolls up EMA alignment,
    # Supertrend & ADX into a single directional read ───────────────────
    if ctx.has_trend_engine:
        bull_trend_votes = sum(bool(v) for v in [ctx.above_ema20, ctx.above_ema50, ctx.above_ema200,
                                                  ctx.ema_aligned_bullish, ctx.supertrend_dir == 1])
        bear_trend_votes = sum(bool(v) for v in [ctx.below_ema20, ctx.below_ema50, ctx.below_ema200,
                                                  ctx.ema_aligned_bearish, ctx.supertrend_dir == -1])
        if bull_trend_votes >= 4 and bool(ctx.adx_strong):
            ctx.index_trend_bias = "🟢 Strong Uptrend (EMA20>50>200, ADX confirmed)"
        elif bear_trend_votes >= 4 and bool(ctx.adx_strong):
            ctx.index_trend_bias = "🔴 Strong Downtrend (EMA20<50<200, ADX confirmed)"
        else:
            ctx.index_trend_bias = "🟡 Index trend unclear / range-bound"
    else:
        ctx.index_trend_bias = "⚪ Index trend unavailable (no candle data)"

    # ── Buyer Bias (STEP 2 / "Option Buyers" layer #10) — requires
    # trend engine for real momentum confirmation; without it, buyer
    # bias stays "Unconfirmed" (weak), which alone is enough to keep
    # CE BUY / PE BUY at WAIT since they require ALL conditions ─────────
    if ctx.has_trend_engine:
        bull_votes = sum(bool(v) for v in [ctx.price_above_vwap, ctx.above_ema20, ctx.above_ema50,
                                            ctx.bullish_structure, ctx.volume_expansion, ctx.adx_strong])
        bear_votes = sum(bool(v) for v in [ctx.price_below_vwap, ctx.below_ema20, ctx.below_ema50,
                                            ctx.bearish_structure, ctx.volume_expansion, ctx.adx_strong])
        if bull_votes >= 5:
            ctx.buyer_bias = "🟢 Strong Bullish Momentum"
        elif bear_votes >= 5:
            ctx.buyer_bias = "🔴 Strong Bearish Momentum"
        else:
            ctx.buyer_bias = "🟡 Momentum Unconfirmed"
    else:
        ctx.buyer_bias = "⚪ Momentum Unconfirmed (no candle data)"

    # ── Overall Market Bias / Expected Direction (rollup across all
    # ten layers, not just the option-chain-local ones) ──────────────────
    bull_signals = sum([
        ctx.pcr_bias.startswith("🟢"), ctx.oi_bias.startswith("🟢"),
        ctx.institutional_bias.startswith("🟢"), ctx.gamma_bias.startswith("🟢"),
        ctx.buyer_bias.startswith("🟢"), ctx.index_trend_bias.startswith("🟢"),
        ctx.global_market_bias.startswith("🟢"), ctx.us_market_bias.startswith("🟢"),
        ctx.sgx_giftnifty_bias.startswith("🟢"), ctx.india_vix_bias.startswith("🟢"),
    ])
    bear_signals = sum([
        ctx.pcr_bias.startswith("🔴"), ctx.oi_bias.startswith("🔴"),
        ctx.institutional_bias.startswith("🔴"), ctx.gamma_bias.startswith("🔴"),
        ctx.buyer_bias.startswith("🔴"), ctx.index_trend_bias.startswith("🔴"),
        ctx.global_market_bias.startswith("🔴"), ctx.us_market_bias.startswith("🔴"),
        ctx.sgx_giftnifty_bias.startswith("🔴"), ctx.india_vix_bias.startswith("🔴"),
    ])
    if bull_signals >= 3 and bull_signals > bear_signals:
        ctx.market_bias = "🟢 Bullish"
        ctx.expected_direction = "🟢 Up-move favored"
    elif bear_signals >= 3 and bear_signals > bull_signals:
        ctx.market_bias = "🔴 Bearish"
        ctx.expected_direction = "🔴 Down-move favored"
    else:
        ctx.market_bias = "🟡 Neutral / Range-bound"
        ctx.expected_direction = "🟡 Range-bound / Unclear"

    return ctx


# ══════════════════════════════════════════════════════════════════════════
# Per-strike condition checklists (STEP 5, STEP 6, STEP 7)
# ══════════════════════════════════════════════════════════════════════════

def _ce_buy_conditions(row: pd.Series, ctx: MarketContext) -> dict:
    """Every condition must be True for a CE BUY signal. Extended with
    index-trend, macro, trap, fake-breakout, liquidity-sweep, gamma and
    premium-behaviour confirmations on top of the original checklist."""
    ce_iv = float(row.get("ce_iv", 0) or 0)
    ce_prem_hist = row.get("ce_ltp_prev")  # optional prior-snapshot premium
    ce_ltp = _safe_float(row.get("ce_ltp"))
    conds = {
        "Price above VWAP": bool(ctx.price_above_vwap),
        "Above 20 EMA": bool(ctx.above_ema20),
        "Above 50 EMA": bool(ctx.above_ema50),
        "Bullish structure (BOS/Order Block)": bool(ctx.bullish_structure),
        "Strong Put Writing at/near strike": float(row.get("pe_chng_oi", 0) or 0) >= ctx.heavy_pe_chng_thresh > 0,
        "Call OI unwinding": float(row.get("ce_chng_oi", 0) or 0) < 0,
        "Increasing volume": float(row.get("ce_volume", 0) or 0) >= ctx.high_ce_vol_thresh > 0,
        "Positive Gamma trend": (float(row.get("Gamma Change", 0) or 0) > 0) if ctx.has_gamma else False,
        "PCR supportive (> 1.0)": ctx.pcr > 1.0,
        "IV acceptable (not elevated)": 0 < ce_iv <= (ctx.ce_iv_high_thresh or ce_iv),
        "No nearby resistance": (ctx.resistance is None) or (row["strike_price"] < ctx.resistance) or
            (ctx.resistance and abs(row["strike_price"] - ctx.resistance) / max(ctx.resistance, 1) > 0.01),
        "Index Trend bullish (EMA20>50>200 + ADX)": ctx.index_trend_bias.startswith("🟢"),
        "Macro layers not contradicting (Global/US/SGX/Sector)": not ctx.macro_contradicts_bulls,
        "India VIX not spiking against buyers": not ctx.india_vix_bias.startswith("🔴"),
        "No Buyer Trap / Fake Breakout detected": not bool(ctx.buyer_trap) and not bool(ctx.fake_breakout),
        "No Stop-Hunt sweep against the move": not bool(ctx.liquidity_sweep_high),
        "CHoCH not bearish": not bool(ctx.choch_bearish),
        "Gamma not flipping bearish": not bool(ctx.gamma_flip_bearish),
        "Fresh buying / Long Build-up (index OI)": ctx.index_oi_signal in ("Long Build-up", "Unavailable"),
        "Premium breakout / volume spike": (
            (ce_prem_hist is not None and ce_ltp > _safe_float(ce_prem_hist) * 1.05) or
            float(row.get("ce_volume", 0) or 0) >= ctx.high_ce_vol_thresh * PREMIUM_VOLUME_SPIKE_MULT
        ) if ce_prem_hist is not None or ctx.high_ce_vol_thresh else False,
    }
    return conds


def _pe_buy_conditions(row: pd.Series, ctx: MarketContext) -> dict:
    """Every condition must be True for a PE BUY signal (mirror of the
    CE BUY checklist, extended the same way)."""
    pe_iv = float(row.get("pe_iv", 0) or 0)
    pe_prem_hist = row.get("pe_ltp_prev")
    pe_ltp = _safe_float(row.get("pe_ltp"))
    conds = {
        "Price below VWAP": bool(ctx.price_below_vwap),
        "Below 20 EMA": bool(ctx.below_ema20),
        "Below 50 EMA": bool(ctx.below_ema50),
        "Bearish structure (BOS/Order Block)": bool(ctx.bearish_structure),
        "Strong Call Writing at/near strike": float(row.get("ce_chng_oi", 0) or 0) >= ctx.heavy_ce_chng_thresh > 0,
        "Put OI unwinding": float(row.get("pe_chng_oi", 0) or 0) < 0,
        "High volume": float(row.get("pe_volume", 0) or 0) >= ctx.high_pe_vol_thresh > 0,
        "Gamma supportive": (float(row.get("Gamma Change", 0) or 0) >= 0) if ctx.has_gamma else False,
        "PCR bearish (< 1.0)": ctx.pcr < 1.0,
        "No nearby support": (ctx.support is None) or (row["strike_price"] > ctx.support) or
            (ctx.support and abs(row["strike_price"] - ctx.support) / max(ctx.support, 1) > 0.01),
        "IV acceptable (not elevated)": 0 < pe_iv <= (ctx.pe_iv_high_thresh or pe_iv),
        "Index Trend bearish (EMA20<50<200 + ADX)": ctx.index_trend_bias.startswith("🔴"),
        "Macro layers not contradicting (Global/US/SGX/Sector)": not ctx.macro_contradicts_bears,
        "India VIX not signalling forced short-covering": not (ctx.india_vix_bias.startswith("🟢") and ctx.india_vix_bias != "⚪ Unavailable" and False),
        "No Seller Trap / Fake Breakdown detected": not bool(ctx.seller_trap) and not bool(ctx.fake_breakdown),
        "No Stop-Hunt sweep against the move": not bool(ctx.liquidity_sweep_low),
        "CHoCH not bullish": not bool(ctx.choch_bullish),
        "Gamma not flipping bullish": not bool(ctx.gamma_flip_bullish),
        "Fresh selling / Short Build-up (index OI)": ctx.index_oi_signal in ("Short Build-up", "Unavailable"),
        "Premium breakout / volume spike": (
            (pe_prem_hist is not None and pe_ltp > _safe_float(pe_prem_hist) * 1.05) or
            float(row.get("pe_volume", 0) or 0) >= ctx.high_pe_vol_thresh * PREMIUM_VOLUME_SPIKE_MULT
        ) if pe_prem_hist is not None or ctx.high_pe_vol_thresh else False,
    }
    return conds


def _ce_sell_conditions(row: pd.Series, ctx: MarketContext) -> dict:
    """Call SELL: requires Theta advantage + IV advantage + Resistance
    confirmation (Call Wall / near resistance) — never sold on
    structure alone."""
    ce_iv = float(row.get("ce_iv", 0) or 0)
    momentum_weak = not bool(ctx.bullish_structure) if ctx.has_trend_engine else True
    near_call_wall = ctx.call_wall is not None and abs(row["strike_price"] - ctx.call_wall) < 1e-6
    iv_rank_ok = ctx.iv_rank_ce is None or ctx.iv_rank_ce >= 60  # sellers want elevated IV rank
    conds = {
        "Strong resistance / Call Wall at strike": near_call_wall or (
            ctx.resistance is not None and abs(row["strike_price"] - ctx.resistance) < 1e-6
        ) or float(row.get("ce_oi", 0) or 0) >= ctx.heavy_ce_oi_thresh > 0,
        "Heavy Call Writing": float(row.get("ce_chng_oi", 0) or 0) >= ctx.heavy_ce_chng_thresh > 0,
        "IV advantage (elevated / high IV rank)": (ce_iv > 0 and ce_iv >= (ctx.ce_iv_high_thresh or ce_iv)) and iv_rank_ok,
        "Momentum weak / stalling": momentum_weak,
        "No Gamma Flip bullish (gamma not accelerating buyers)": not bool(ctx.gamma_flip_bullish),
        "Theta advantage (decay favors seller)": True,  # structural — every session decays premium once IV/resistance hold
        "Smart Money / Dealer / Seller Bias not against the sell": not (
            ctx.smart_money_bias.startswith("🟢") and ctx.dealer_bias.startswith("🟢") and ctx.seller_bias.startswith("🟢")
        ),
    }
    return conds


def _pe_sell_conditions(row: pd.Series, ctx: MarketContext) -> dict:
    """Put SELL: requires Theta advantage + IV advantage + Support
    confirmation (Put Wall / near support)."""
    pe_iv = float(row.get("pe_iv", 0) or 0)
    momentum_weak = not bool(ctx.bearish_structure) if ctx.has_trend_engine else True
    near_put_wall = ctx.put_wall is not None and abs(row["strike_price"] - ctx.put_wall) < 1e-6
    iv_rank_ok = ctx.iv_rank_pe is None or ctx.iv_rank_pe >= 60
    conds = {
        "Strong support / Put Wall at strike": near_put_wall or (
            ctx.support is not None and abs(row["strike_price"] - ctx.support) < 1e-6
        ) or float(row.get("pe_oi", 0) or 0) >= ctx.heavy_pe_oi_thresh > 0,
        "Heavy Put Writing": float(row.get("pe_chng_oi", 0) or 0) >= ctx.heavy_pe_chng_thresh > 0,
        "IV advantage (elevated / high IV rank)": (pe_iv > 0 and pe_iv >= (ctx.pe_iv_high_thresh or pe_iv)) and iv_rank_ok,
        "Momentum weak / stalling": momentum_weak,
        "No Gamma Flip bearish (gamma not accelerating sellers of premium)": not bool(ctx.gamma_flip_bearish),
        "Theta advantage (decay favors seller)": True,
        "Smart Money / Dealer / Seller Bias not against the sell": not (
            ctx.smart_money_bias.startswith("🔴") and ctx.dealer_bias.startswith("🔴") and ctx.seller_bias.startswith("🔴")
        ),
    }
    return conds


# ══════════════════════════════════════════════════════════════════════════
# Weighted probability / confidence / risk scoring
# ══════════════════════════════════════════════════════════════════════════

def _frac(conds: dict, keys: list) -> float:
    """Fraction of the named keys that are True; keys missing from
    `conds` are ignored (so a category never falsely fails just
    because one signal wasn't relevant to this checklist)."""
    present = [k for k in keys if k in conds]
    if not present:
        return 0.5  # unavailable -> neutral, never a free pass
    return sum(1 for k in present if conds[k]) / len(present)


def _category_scores(conds: dict, ctx: MarketContext, base_score: float, direction: str) -> dict:
    """Buckets the checklist + context into the ten weighted scoring
    categories used for the final probability (Trend 20%, OI 20%,
    Price Action 15%, Volume 10%, Greeks 10%, PCR 5%, IV 5%,
    Dealer 5%, Institutional 5%, Smart Money 5%)."""
    is_ce = direction.startswith("CE")
    trend_keys = (["Price above VWAP", "Above 20 EMA", "Above 50 EMA",
                   "Bullish structure (BOS/Order Block)", "Index Trend bullish (EMA20>50>200 + ADX)"]
                  if is_ce else
                  ["Price below VWAP", "Below 20 EMA", "Below 50 EMA",
                   "Bearish structure (BOS/Order Block)", "Index Trend bearish (EMA20<50<200 + ADX)"])
    oi_keys = (["Strong Put Writing at/near strike", "Call OI unwinding",
                "Fresh buying / Long Build-up (index OI)"] if is_ce and "BUY" in direction else
               ["Strong Call Writing at/near strike", "Put OI unwinding",
                "Fresh selling / Short Build-up (index OI)"] if not is_ce and "BUY" in direction else
               ["Heavy Call Writing"] if direction == "CE SELL" else ["Heavy Put Writing"])
    price_action_keys = ["No Buyer Trap / Fake Breakout detected", "No Stop-Hunt sweep against the move",
                          "CHoCH not bearish", "No nearby resistance",
                          "No Seller Trap / Fake Breakdown detected", "CHoCH not bullish", "No nearby support",
                          "Strong resistance / Call Wall at strike", "Strong support / Put Wall at strike"]
    volume_keys = ["Increasing volume", "High volume", "Premium breakout / volume spike"]
    greeks_keys = ["Positive Gamma trend", "Gamma supportive", "No Gamma Flip bullish (gamma not accelerating buyers)",
                   "No Gamma Flip bearish (gamma not accelerating sellers of premium)",
                   "Gamma not flipping bearish", "Gamma not flipping bullish"]
    pcr_keys = ["PCR supportive (> 1.0)", "PCR bearish (< 1.0)"]
    iv_keys = ["IV acceptable (not elevated)", "IV advantage (elevated / high IV rank)"]

    scores = {
        "trend": _frac(conds, trend_keys),
        "oi": _frac(conds, oi_keys),
        "price_action": _frac(conds, price_action_keys),
        "volume": _frac(conds, volume_keys),
        "greeks": _frac(conds, greeks_keys),
        "pcr": _frac(conds, pcr_keys),
        "iv": _frac(conds, iv_keys),
        "dealer": 1.0 if (ctx.dealer_bias.startswith("🟢") and is_ce) or (ctx.dealer_bias.startswith("🔴") and not is_ce) else
                  (0.5 if ctx.dealer_bias.startswith("🟡") else 0.0),
        "institutional": 1.0 if (ctx.institutional_bias.startswith("🟢") and is_ce) or (ctx.institutional_bias.startswith("🔴") and not is_ce) else
                         (0.5 if ctx.institutional_bias.startswith("🟡") else 0.0),
        "smart_money": 1.0 if (ctx.smart_money_bias.startswith("🟢") and is_ce) or (ctx.smart_money_bias.startswith("🔴") and not is_ce) else
                       (0.5 if ctx.smart_money_bias.startswith("🟡") else 0.0),
    }
    return scores


def _weighted_probability(conds: dict, ctx: MarketContext, base_score: float, direction: str) -> tuple[float, dict]:
    """STEP 9 weighted scoring — blends the strike's own quantitative
    model score (CE/PE Score, 0-100) into the 'trend'/'oi' buckets via
    base_score, then combines every category with CATEGORY_WEIGHTS.
    Returns (probability_0_100, category_scores) for transparency."""
    cats = _category_scores(conds, ctx, base_score, direction)
    # Blend each of the two heaviest categories with the model's own
    # 0-100 score so a strong quantitative read still matters.
    cats["trend"] = _clip01(0.7 * cats["trend"] + 0.3 * (base_score / 100.0))
    cats["oi"] = _clip01(0.7 * cats["oi"] + 0.3 * (base_score / 100.0))
    probability = sum(cats[k] * w for k, w in CATEGORY_WEIGHTS.items())
    return round(_clip01(probability) * 100, 1), cats


def _confidence_from_categories(cats: dict, base_score: float) -> float:
    """Confidence is the weighted-confirmation strength itself (not a
    simple average with the raw score) — how many of the ten
    weighted buckets are actually confirming, scaled 0-100."""
    strength = sum(cats[k] * w for k, w in CATEGORY_WEIGHTS.items())
    return round(_clip01(0.6 * strength + 0.4 * (base_score / 100.0)) * 100, 1)


def _risk_level(probability: float, confidence: float, iv_bias: str) -> str:
    score = (probability + confidence) / 2
    if iv_bias.startswith("🔴"):
        score -= 10  # elevated IV = elevated risk even with a good setup
    if score >= 90:
        return "Low"
    if score >= 80:
        return "Moderate"
    if score >= 70:
        return "Elevated"
    return "High"


def _levels(entry: float):
    """Fallback percentage-based levels (used when ATR/delta are not
    available). T2 is deliberately calibrated so RR at T2 = 2.0,
    satisfying the 1:2 minimum Reward:Risk floor by construction."""
    if entry <= 0:
        return 0.0, 0.0, 0.0, 0.0
    sl = round(entry * 0.85, 2)
    t1 = round(entry * 1.15, 2)
    t2 = round(entry * 1.30, 2)
    t3 = round(entry * 1.50, 2)
    return sl, t1, t2, t3


def _dynamic_levels(entry: float, ctx: MarketContext, row: pd.Series, is_ce: bool,
                     is_sell: bool) -> tuple[float, float, float, float, float]:
    """Dynamic Stop Loss via ATR (translated through option delta into
    premium terms) and dynamic Targets from the nearest Support/
    Resistance, with a fallback to the calibrated percentage levels
    when ATR/delta aren't available. Always returns (sl, t1, t2, t3, rr)
    where rr is the Reward:Risk ratio measured at T2."""
    delta = row.get("delta")
    atr = ctx.atr
    if entry <= 0:
        return 0.0, 0.0, 0.0, 0.0, 0.0

    if atr and delta not in (None, 0) and not is_sell:
        delta = abs(_safe_float(delta, 0.5)) or 0.5
        risk_premium = max(atr * delta, entry * 0.05)
        nearest_level = ctx.resistance if is_ce else ctx.support
        if nearest_level and ctx.spot_price:
            underlying_room = abs(nearest_level - ctx.spot_price)
            reward_premium = max(underlying_room * delta, risk_premium * MIN_RISK_REWARD)
        else:
            reward_premium = risk_premium * MIN_RISK_REWARD
        sl = round(max(entry - risk_premium, entry * 0.5), 2)
        t2 = round(entry + reward_premium, 2)
        t1 = round(entry + reward_premium * 0.5, 2)
        t3 = round(entry + reward_premium * 1.6, 2)
        risk = entry - sl
        reward = t2 - entry
    elif atr and delta not in (None, 0) and is_sell:
        delta = abs(_safe_float(delta, 0.5)) or 0.5
        risk_premium = max(atr * delta, entry * 0.10)
        reward_premium = risk_premium * MIN_RISK_REWARD
        sl = round(entry + risk_premium, 2)
        t2 = round(max(entry - reward_premium, 0.0), 2)
        t1 = round(max(entry - reward_premium * 0.5, 0.0), 2)
        t3 = round(max(entry - reward_premium * 1.6, 0.0), 2)
        risk = sl - entry
        reward = entry - t2
    elif is_sell:
        sl = round(entry * 1.20, 2)
        t1 = round(entry * 0.85, 2)
        t2 = round(entry * 0.70, 2)
        t3 = round(entry * 0.50, 2)
        risk = sl - entry
        reward = entry - t2
    else:
        sl, t1, t2, t3 = _levels(entry)
        risk = entry - sl
        reward = t2 - entry

    rr = round(reward / risk, 2) if risk > 0 else 0.0
    return sl, t1, t2, t3, rr


# ══════════════════════════════════════════════════════════════════════════
# Per-strike evaluation (STEP 8 traps + STEP 9 final decision engine)
# ══════════════════════════════════════════════════════════════════════════

def _evaluate_strike(row: pd.Series, ctx: MarketContext,
                      min_probability: float, min_confidence: float) -> dict:
    strike = float(row["strike_price"])
    ce_score = float(row.get("CE Score", 0) or 0)
    pe_score = float(row.get("PE Score", 0) or 0)

    ce_buy_conds = _ce_buy_conditions(row, ctx)
    pe_buy_conds = _pe_buy_conditions(row, ctx)
    ce_sell_conds = _ce_sell_conditions(row, ctx)
    pe_sell_conds = _pe_sell_conditions(row, ctx)

    ce_buy_all = ctx.has_trend_engine and all(ce_buy_conds.values())
    pe_buy_all = ctx.has_trend_engine and all(pe_buy_conds.values())
    ce_sell_all = all(ce_sell_conds.values())
    pe_sell_all = all(pe_sell_conds.values())

    ce_buy_prob, ce_buy_cats = _weighted_probability(ce_buy_conds, ctx, ce_score, "CE BUY")
    pe_buy_prob, pe_buy_cats = _weighted_probability(pe_buy_conds, ctx, pe_score, "PE BUY")
    ce_sell_prob, ce_sell_cats = _weighted_probability(ce_sell_conds, ctx, 100 - ce_score, "CE SELL")
    pe_sell_prob, pe_sell_cats = _weighted_probability(pe_sell_conds, ctx, 100 - pe_score, "PE SELL")

    ce_buy_conf = _confidence_from_categories(ce_buy_cats, ce_score)
    pe_buy_conf = _confidence_from_categories(pe_buy_cats, pe_score)
    ce_sell_conf = _confidence_from_categories(ce_sell_cats, 100 - ce_score)
    pe_sell_conf = _confidence_from_categories(pe_sell_cats, 100 - pe_score)

    # Expiry-day rules: buyers face amplified theta risk (raise the
    # bar); sellers get the benefit of an accelerated decay edge
    # (small relaxation), consistent with weekly vs monthly expiry.
    buy_prob_bar, sell_prob_bar = min_probability, min_probability
    if ctx.is_expiry_day:
        buy_prob_bar += 5.0
        sell_prob_bar = max(sell_prob_bar - 3.0, 70.0)
    elif ctx.expiry_type == "monthly":
        buy_prob_bar -= 1.0  # less gamma risk than a weekly expiry

    # STEP 8 — avoid-trap / disagreement gate: never allow a trade
    # through if Smart Money, Dealer Bias and Seller Bias all disagree
    # with it, even if the local strike checklist happened to pass.
    if ce_buy_all and ctx.institutional_bias.startswith("🔴") and ctx.smart_money_bias.startswith("🔴"):
        ce_buy_all = False
    if pe_buy_all and ctx.institutional_bias.startswith("🟢") and ctx.smart_money_bias.startswith("🟢"):
        pe_buy_all = False
    if ce_buy_all and ctx.smart_money_bias.startswith("🔴") and ctx.dealer_bias.startswith("🔴") and ctx.seller_bias.startswith("🔴"):
        ce_buy_all = False
    if pe_buy_all and ctx.smart_money_bias.startswith("🟢") and ctx.dealer_bias.startswith("🟢") and ctx.seller_bias.startswith("🟢"):
        pe_buy_all = False

    candidates = []
    if ce_buy_all and ce_buy_prob >= buy_prob_bar and ce_buy_conf >= min_confidence:
        candidates.append(("CE BUY", ce_buy_prob, ce_buy_conf, ce_buy_conds, row.get("ce_ltp", 0), False))
    if pe_buy_all and pe_buy_prob >= buy_prob_bar and pe_buy_conf >= min_confidence:
        candidates.append(("PE BUY", pe_buy_prob, pe_buy_conf, pe_buy_conds, row.get("pe_ltp", 0), False))
    if ce_sell_all and ce_sell_prob >= sell_prob_bar:
        candidates.append(("CE SELL", ce_sell_prob, ce_sell_conf, ce_sell_conds, row.get("ce_ltp", 0), True))
    if pe_sell_all and pe_sell_prob >= sell_prob_bar:
        candidates.append(("PE SELL", pe_sell_prob, pe_sell_conf, pe_sell_conds, row.get("pe_ltp", 0), True))

    # Capital-protection RR filter: compute dynamic levels for every
    # candidate and reject anything below the 1:2 Reward:Risk floor
    # BEFORE picking a winner, rather than after.
    viable = []
    for action, prob, conf, conds, ltp, is_sell in candidates:
        ltp = float(ltp or 0)
        is_ce = action.startswith("CE")
        sl, t1, t2, t3, rr = _dynamic_levels(ltp, ctx, row, is_ce, is_sell)
        if rr >= MIN_RISK_REWARD:
            viable.append((action, prob, conf, conds, ltp, is_sell, sl, t1, t2, t3, rr))

    if not viable:
        return {
            "Strike": strike, "Recommended Action": "WAIT",
            "Probability %": max(ce_buy_prob, pe_buy_prob, ce_sell_prob, pe_sell_prob),
            "Confidence %": max(ce_buy_conf, pe_buy_conf, ce_sell_conf, pe_sell_conf),
            "Risk Level": "—", "Entry": 0.0, "Stop Loss": 0.0,
            "Target 1": 0.0, "Target 2": 0.0, "Target 3": 0.0,
            "Reason for Trade": "—",
            "Reason to Avoid Trade": _missing_reasons(ce_buy_conds, pe_buy_conds, ctx, candidates, len(candidates) > 0),
            "Invalidation Level": "—",
            "_ce_buy_conditions": ce_buy_conds, "_pe_buy_conditions": pe_buy_conds,
            "_ce_sell_conditions": ce_sell_conds, "_pe_sell_conditions": pe_sell_conds,
        }

    action, prob, conf, conds, ltp, is_sell, sl, t1, t2, t3, rr = max(
        viable, key=lambda c: (c[1] + c[2])
    )
    entry = ltp

    invalidation = None
    if action == "CE BUY":
        invalidation = ctx.resistance if ctx.resistance else strike
    elif action == "PE BUY":
        invalidation = ctx.support if ctx.support else strike
    elif action == "CE SELL":
        invalidation = ctx.resistance
    elif action == "PE SELL":
        invalidation = ctx.support

    return {
        "Strike": strike, "Recommended Action": action,
        "Probability %": prob, "Confidence %": conf,
        "Risk Level": _risk_level(prob, conf, ctx.iv_bias),
        "Entry": entry, "Stop Loss": sl,
        "Target 1": t1, "Target 2": t2, "Target 3": t3,
        "Reason for Trade": " · ".join(k for k, v in conds.items() if v) + f" · RR {rr}:1",
        "Reason to Avoid Trade": _missing_reasons(
            _ce_buy_conditions(row, ctx) if action != "CE BUY" else {},
            _pe_buy_conditions(row, ctx) if action != "PE BUY" else {}, ctx, [], False),
        "Invalidation Level": f"{invalidation:,.0f}" if invalidation else "—",
        "_ce_buy_conditions": ce_buy_conds, "_pe_buy_conditions": pe_buy_conds,
        "_ce_sell_conditions": ce_sell_conds, "_pe_sell_conditions": pe_sell_conds,
    }


def _missing_reasons(ce_buy_conds: dict, pe_buy_conds: dict, ctx: MarketContext,
                      candidates: list, had_candidates_but_rr_failed: bool) -> str:
    missing = []
    if not ctx.has_trend_engine:
        missing.append("No underlying candle data (VWAP/EMA/Structure unconfirmed)")
    if not ctx.macro_available:
        missing.append("Macro layers unavailable (Global/VIX/SGX/US/Sector)")
    if had_candidates_but_rr_failed:
        missing.append(f"Candidate found but Reward:Risk below 1:{MIN_RISK_REWARD:.0f} floor — rejected for capital protection")
    missing += [f"CE: {k}" for k, v in ce_buy_conds.items() if not v]
    missing += [f"PE: {k}" for k, v in pe_buy_conds.items() if not v]
    if not missing:
        return "—"
    return " · ".join(missing[:6]) + (" · …" if len(missing) > 6 else "")


# ══════════════════════════════════════════════════════════════════════════
# PUBLIC ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════

def analyze_market(df: pd.DataFrame, spot_price: float, atm_strike: float,
                    max_pain: float, pcr: float, support: Optional[float] = None,
                    resistance: Optional[float] = None, trend_engine: Optional[dict] = None,
                    expiry_label: str = "", min_probability: float = MIN_PROBABILITY,
                    min_confidence: float = MIN_CONFIDENCE, top_n: int = 10,
                    oi_history: Optional[dict] = None,
                    macro_context: Optional[dict] = None) -> dict:
    """
    Main entry point. Runs the full ten-layer decision framework —

        1 Global Market -> 2 India VIX -> 3 SGX/GIFT Nifty -> 4 US Market
        -> 5 Sector Strength -> 6 Index Trend -> 7 Smart Money
        -> 8 Dealer Positioning -> 9 Option Sellers -> 10 Option Buyers

    — then the strict per-strike CE BUY / PE BUY / CE SELL / PE SELL
    checklists, weighted probability/confidence scoring, and a 1:2
    minimum Reward:Risk capital-protection filter, and returns a dict
    matching the original "FINAL OUTPUT FORMAT":

        {
          "market_bias", "smart_money_bias", "seller_bias", "buyer_bias",
          "institutional_bias", "dealer_bias", "pcr_bias", "oi_bias",
          "gamma_bias", "iv_bias", "expected_direction",
          "best_trade": {...} | None,
          "top_trades": [ {...}, ... ],   # up to top_n qualifying trades
          "all_strikes": [ {...}, ... ],  # every strike, mostly WAIT
          "data_quality": {...},
          "decision_layers": {...},       # NEW — additive, ordered 1-10 read
          "signal_engine_meta": {...},    # NEW — additive, weights/RR/expiry info
        }

    `df` must already carry the dashboard's own CE Score / PE Score
    columns (i.e. call this AFTER compute_ai_engine()). If those
    columns are missing, this degrades gracefully — every strike
    simply scores lower on the weighted probability blend and is far
    less likely to clear the minimum bar, which is the correct
    fail-safe direction.

    `oi_history` and `macro_context` are OPTIONAL additions. Existing
    call sites that only pass the original arguments are entirely
    unaffected; the new layers simply report as unavailable and can
    only ever add caution, never manufacture a signal.
    """
    if df is None or df.empty:
        return _empty_result(reason="Empty option chain — nothing to analyze.")

    ctx = build_market_context(df, spot_price, atm_strike, max_pain, pcr, support,
                                resistance, trend_engine=trend_engine, expiry_label=expiry_label,
                                oi_history=oi_history, macro_context=macro_context)

    results = []
    for _, row in df.iterrows():
        try:
            results.append(_evaluate_strike(row, ctx, min_probability, min_confidence))
        except Exception:  # noqa: BLE001 - never let one malformed row kill the whole read
            continue

    qualifying = [r for r in results if r["Recommended Action"] != "WAIT"]
    qualifying.sort(key=lambda r: (r["Probability %"] + r["Confidence %"]), reverse=True)
    best_trade = qualifying[0] if qualifying else None

    for r in results:
        r.pop("_ce_buy_conditions", None)
        r.pop("_pe_buy_conditions", None)
        r.pop("_ce_sell_conditions", None)
        r.pop("_pe_sell_conditions", None)

    return {
        "market_bias": ctx.market_bias,
        "smart_money_bias": ctx.smart_money_bias,
        "seller_bias": ctx.seller_bias,
        "buyer_bias": ctx.buyer_bias,
        "institutional_bias": ctx.institutional_bias,
        "dealer_bias": ctx.dealer_bias,
        "pcr_bias": ctx.pcr_bias,
        "oi_bias": ctx.oi_bias,
        "gamma_bias": ctx.gamma_bias,
        "iv_bias": ctx.iv_bias,
        "expected_direction": ctx.expected_direction,
        "best_trade": best_trade,
        "top_trades": qualifying[:top_n],
        "all_strikes": results,
        "data_quality": {
            "has_trend_engine": ctx.has_trend_engine,
            "has_gamma": ctx.has_gamma,
            "strikes_evaluated": len(results),
            "strikes_qualifying": len(qualifying),
            "note": (
                "Buyer-side (CE BUY / PE BUY) signals require underlying VWAP/EMA/structure "
                "confirmation from a trend_engine (e.g. this dashboard's compute_scalping_trend_engine "
                "output) AND the Index Trend / Macro / Smart-Money / Dealer / Seller layers to agree. "
                "Without them, buyer-side signals correctly stay at WAIT per the framework's "
                "'missing confirmation -> WAIT' rule; seller-side (CE SELL / PE SELL) reads can still "
                "surface from the option-chain snapshot alone provided Theta + IV + Resistance/Support "
                "confirmation all hold, and are rejected outright if Reward:Risk is below 1:2."
            ),
        },
        # ── Additive keys (do not remove or rename anything above) ──────
        "decision_layers": {
            "order": DECISION_LAYER_ORDER,
            "1_global_market": ctx.global_market_bias,
            "2_india_vix": ctx.india_vix_bias,
            "3_sgx_giftnifty": ctx.sgx_giftnifty_bias,
            "4_us_market": ctx.us_market_bias,
            "5_sector_strength": ctx.sector_strength_bias,
            "6_index_trend": ctx.index_trend_bias,
            "7_smart_money": ctx.smart_money_bias,
            "8_dealer_positioning": ctx.dealer_bias,
            "9_option_sellers": ctx.seller_bias,
            "10_option_buyers": ctx.buyer_bias,
            "macro_available": ctx.macro_available,
            "oi_shift_available": ctx.oi_shift_available,
            "index_oi_signal": ctx.index_oi_signal,
            "max_pain_shift": ctx.max_pain_shift_bias,
            "call_wall": ctx.call_wall,
            "put_wall": ctx.put_wall,
            "zero_gamma_strike": ctx.zero_gamma_strike,
            "pin_risk": ctx.pin_risk,
            "iv_rank_ce": ctx.iv_rank_ce,
            "iv_rank_pe": ctx.iv_rank_pe,
            "iv_percentile_ce": ctx.iv_percentile_ce,
            "iv_percentile_pe": ctx.iv_percentile_pe,
            "iv_expansion": ctx.iv_expansion,
            "iv_crush": ctx.iv_crush,
            "gamma_flip_bullish": ctx.gamma_flip_bullish,
            "gamma_flip_bearish": ctx.gamma_flip_bearish,
            "buyer_trap": ctx.buyer_trap,
            "seller_trap": ctx.seller_trap,
            "fake_breakout": ctx.fake_breakout,
            "fake_breakdown": ctx.fake_breakdown,
            "liquidity_sweep_high": ctx.liquidity_sweep_high,
            "liquidity_sweep_low": ctx.liquidity_sweep_low,
            "open_eq_high": ctx.open_eq_high,
            "open_eq_low": ctx.open_eq_low,
            "vwap_rejection": ctx.vwap_rejection,
            "vwap_reclaim": ctx.vwap_reclaim,
        },
        "signal_engine_meta": {
            "category_weights": CATEGORY_WEIGHTS,
            "min_risk_reward": MIN_RISK_REWARD,
            "min_probability_bar": min_probability,
            "min_confidence_bar": min_confidence,
            "expiry_type": ctx.expiry_type,
            "is_expiry_day": ctx.is_expiry_day,
        },
    }


def _empty_result(reason: str) -> dict:
    return {
        "market_bias": "🟡 Neutral", "smart_money_bias": "🟡 Neutral",
        "seller_bias": "🟡 Balanced", "buyer_bias": "🟡 Weak",
        "institutional_bias": "🟡 Neutral", "dealer_bias": "🟡 Neutral",
        "pcr_bias": "🟡 Neutral", "oi_bias": "🟡 Neutral",
        "gamma_bias": "⚪ Unavailable", "iv_bias": "🟡 Neutral",
        "expected_direction": "🟡 Range-bound", "best_trade": None,
        "top_trades": [], "all_strikes": [],
        "data_quality": {"has_trend_engine": False, "has_gamma": False,
                          "strikes_evaluated": 0, "strikes_qualifying": 0, "note": reason},
        "decision_layers": {"order": DECISION_LAYER_ORDER, "note": reason},
        "signal_engine_meta": {"category_weights": CATEGORY_WEIGHTS, "min_risk_reward": MIN_RISK_REWARD},
    }


# ══════════════════════════════════════════════════════════════════════════
# Optional Streamlit rendering helper (mirrors this dashboard's card style)
# ══════════════════════════════════════════════════════════════════════════

def render_ai_analysis_block(result: dict, st_module=None) -> None:
    """Renders the analyze_market() result using the same intel-card /
    rating-* CSS classes already defined in the dashboard's <style> block,
    so it drops into show_option_chain() without adding new CSS. Pass the
    imported `streamlit` module in as `st_module` (kept as a parameter,
    not a hard import, so this file has no Streamlit dependency at
    import time and can be unit-tested headlessly)."""
    if st_module is None:
        import streamlit as st_module  # local import keeps module import light

    st_module.markdown('<div class="block-title">🧠 AI Market Analysis (10-Layer: Global → VIX → SGX/US → Sector → '
                        'Index Trend → Smart Money → Dealer → Sellers → Buyers)</div>',
                        unsafe_allow_html=True)

    bias_fields = [
        ("Market Bias", result["market_bias"]), ("Expected Direction", result["expected_direction"]),
        ("Seller Bias", result["seller_bias"]), ("Buyer Bias", result["buyer_bias"]),
        ("Smart Money Bias", result["smart_money_bias"]), ("Institutional Bias", result["institutional_bias"]),
        ("Dealer Bias", result["dealer_bias"]), ("PCR Bias", result["pcr_bias"]),
        ("OI Bias", result["oi_bias"]), ("Gamma Bias", result["gamma_bias"]), ("IV Bias", result["iv_bias"]),
    ]
    layers = result.get("decision_layers", {})
    if layers:
        bias_fields.append(("Index Trend", layers.get("6_index_trend", "—")))
        bias_fields.append(("Global Market", layers.get("1_global_market", "—")))
        bias_fields.append(("India VIX", layers.get("2_india_vix", "—")))

    cols = st_module.columns(4)
    for i, (label, value) in enumerate(bias_fields):
        with cols[i % 4]:
            st_module.markdown(
                f"""<div class="intel-card"><div class="intel-label">{label}</div>
                <div class="intel-value" style="font-size:14px;">{value}</div></div>""",
                unsafe_allow_html=True,
            )

    st_module.markdown("<br>", unsafe_allow_html=True)
    best = result.get("best_trade")
    if not best:
        st_module.info(
            "🟡 WAIT — no strike currently satisfies every condition in the ten-layer Global/VIX/SGX/US/Sector/"
            "Index-Trend/Smart-Money/Dealer/Seller/Buyer checklist at the required probability/confidence bar, "
            "or the best candidate failed the 1:2 Reward:Risk capital-protection floor. " +
            result.get("data_quality", {}).get("note", "")
        )
        return

    action = best["Recommended Action"]
    css = "rating-strongbuy" if "BUY" in action else "rating-avoid"
    st_module.markdown(f"""
    <div class="intel-card">
      <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;">
        <div><b style="color:#e6edf3;">{best['Strike']:,.0f}</b>
          &nbsp; <span class="{css}">{action}</span></div>
        <div class="intel-label">Probability <span style="color:#e6edf3;font-weight:700;">{best['Probability %']:.1f}%</span>
          &nbsp;|&nbsp; Confidence <span style="color:#e6edf3;font-weight:700;">{best['Confidence %']:.1f}%</span></div>
      </div>
      <div style="margin-top:10px;font-family:'Courier New',monospace;color:#e6edf3;font-size:14px;">
        Entry <b>{best['Entry']}</b> &nbsp;|&nbsp; Stop Loss <b>{best['Stop Loss']}</b> &nbsp;|&nbsp;
        T1 {best['Target 1']} &nbsp; T2 {best['Target 2']} &nbsp; T3 {best['Target 3']}
        &nbsp;|&nbsp; Risk {best['Risk Level']} &nbsp;|&nbsp; Invalidation {best['Invalidation Level']}
      </div>
      <div style="margin-top:8px;color:#8b949e;font-size:12px;">Reason: {best['Reason for Trade']}</div>
    </div>
    """, unsafe_allow_html=True)

    if result.get("top_trades"):
        st_module.markdown("<br>**Other qualifying strikes**", unsafe_allow_html=True)
        st_module.dataframe(
            pd.DataFrame(result["top_trades"]).drop(columns=["Reason to Avoid Trade"], errors="ignore"),
            use_container_width=True, height=280,
        )
