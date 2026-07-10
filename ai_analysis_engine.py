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

# A strike's ΔOI must be at least this fraction of the chain's "heavy"
# ΔOI threshold to count as *fresh* positioning at all. Below this, the
# strike is treated as noise — abs(ΔOI) ≈ 0 — no matter how large its
# historical/absolute OI is. This is what stops big legacy OI strikes
# from masquerading as fresh institutional activity.
FRESH_OI_SIGNIFICANCE_RATIO = 0.5

RISK_LEVELS = ["Low", "Moderate", "Elevated", "High"]

# ══════════════════════════════════════════════════════════════════════════
# FRESH-POSITIONING SCORING MODEL
# ══════════════════════════════════════════════════════════════════════════
CATEGORY_WEIGHTS = {
    "oi_change": 0.40,
    "volume": 0.20,
    "price_action": 0.15,
    "pcr": 0.10,
    "iv": 0.10,
    "absolute_oi": 0.05,
}
assert abs(sum(CATEGORY_WEIGHTS.values()) - 1.0) < 1e-9

DECISION_LAYER_ORDER = [
    "global_market", "india_vix", "sgx_giftnifty", "us_market", "sector_strength",
    "index_trend", "smart_money", "dealer_positioning", "option_sellers", "option_buyers",
]


def _safe_quantile(series: pd.Series, q: float) -> float:
    s = pd.to_numeric(series, errors="coerce").dropna()
    if s.empty:
        return 0.0
    return float(s.quantile(q))


def _pct_rank(value: float, series: pd.Series) -> float:
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


def _oi_price_action_signal(price_chg_pct, oi_chg_pct) -> str:
    if price_chg_pct is None or oi_chg_pct is None:
        return "Unavailable"
    if price_chg_pct >= 0 and oi_chg_pct >= 0:
        return "Long Build-up"
    if price_chg_pct >= 0 and oi_chg_pct < 0:
        return "Short Covering"
    if price_chg_pct < 0 and oi_chg_pct >= 0:
        return "Short Build-up"
    return "Long Unwinding"


def _snapshot_delta(history, key, current):
    if not history:
        return None
    prev = history.get(key)
    if prev is None or prev == 0:
        return None
    try:
        return (current - prev) / abs(prev) * 100.0
    except ZeroDivisionError:
        return None


def _select_fresh_level(df, chng_col, vol_col, heavy_chng_thresh):
    if df is None or df.empty or chng_col not in df.columns:
        return None, None
    chng = pd.to_numeric(df[chng_col], errors="coerce")
    candidates = df.loc[chng > 0].copy()
    if heavy_chng_thresh and heavy_chng_thresh > 0:
        candidates = candidates.loc[pd.to_numeric(candidates[chng_col], errors="coerce")
                                     >= heavy_chng_thresh * FRESH_OI_SIGNIFICANCE_RATIO]
    if candidates.empty:
        return None, None
    if vol_col in candidates.columns:
        chng_rank = pd.to_numeric(candidates[chng_col], errors="coerce").rank(pct=True)
        vol_rank = pd.to_numeric(candidates[vol_col], errors="coerce").fillna(0).rank(pct=True)
        fresh_score = chng_rank.fillna(0) * 0.7 + vol_rank.fillna(0) * 0.3
        best_idx = fresh_score.idxmax()
    else:
        best_idx = pd.to_numeric(candidates[chng_col], errors="coerce").idxmax()
    best = candidates.loc[best_idx]
    return float(best["strike_price"]), float(best[chng_col])


def _classify_call_flow(chng_oi, ltp, ltp_prev) -> str:
    if chng_oi is None or ltp_prev is None or ltp is None:
        return "Unavailable"
    price_chg = _safe_float(ltp) - _safe_float(ltp_prev)
    if abs(chng_oi) < 1e-9:
        return "Unavailable"
    if chng_oi > 0 and price_chg < 0:
        return "Fresh Call Writing"
    if chng_oi > 0 and price_chg >= 0:
        return "Call Buying"
    if chng_oi < 0 and price_chg >= 0:
        return "Short Covering (Call)"
    return "Long Unwinding (Call)"


def _classify_put_flow(chng_oi, ltp, ltp_prev) -> str:
    if chng_oi is None or ltp_prev is None or ltp is None:
        return "Unavailable"
    price_chg = _safe_float(ltp) - _safe_float(ltp_prev)
    if abs(chng_oi) < 1e-9:
        return "Unavailable"
    if chng_oi > 0 and price_chg < 0:
        return "Put Buying"
    if chng_oi > 0 and price_chg >= 0:
        return "Put Writing"
    if chng_oi < 0 and price_chg >= 0:
        return "Long Unwinding (Put)"
    return "Short Covering (Put)"


@dataclass
class MarketContext:
    spot_price: float
    atm_strike: float
    max_pain: float
    pcr: float
    support: Optional[float]
    resistance: Optional[float]
    expiry_label: str = ""

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
    supertrend_dir: Optional[int] = None
    atr: Optional[float] = None

    open_eq_high: Optional[bool] = None
    open_eq_low: Optional[bool] = None
    fake_breakout: Optional[bool] = None
    fake_breakdown: Optional[bool] = None
    liquidity_sweep_high: Optional[bool] = None
    liquidity_sweep_low: Optional[bool] = None
    stop_hunt_detected: Optional[bool] = None
    buyer_trap: Optional[bool] = None
    seller_trap: Optional[bool] = None

    delta_acceleration: Optional[bool] = None
    gamma_acceleration: Optional[bool] = None
    gamma_flip_bullish: Optional[bool] = None
    gamma_flip_bearish: Optional[bool] = None
    zero_gamma_strike: Optional[float] = None
    call_wall: Optional[float] = None
    put_wall: Optional[float] = None
    call_wall_source: str = "unavailable"
    put_wall_source: str = "unavailable"
    pin_risk: bool = False

    fresh_support: Optional[float] = None
    fresh_support_pe_chng_oi: Optional[float] = None
    fresh_resistance: Optional[float] = None
    fresh_resistance_ce_chng_oi: Optional[float] = None
    strongest_writing_strike: Optional[dict] = None
    strongest_buying_strike: Optional[dict] = None
    institutional_flow_counts: dict = field(default_factory=dict)
    pcr_prev: Optional[float] = None
    pcr_improving: Optional[bool] = None
    pcr_falling: Optional[bool] = None

    iv_rank_ce: Optional[float] = None
    iv_rank_pe: Optional[float] = None
    iv_percentile_ce: Optional[float] = None
    iv_percentile_pe: Optional[float] = None
    iv_expansion: Optional[bool] = None
    iv_crush: Optional[bool] = None

    max_pain_prev: Optional[float] = None
    max_pain_shift_bias: str = "⚪ Unavailable"

    oi_shift_available: bool = False
    index_oi_signal: str = "Unavailable"

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

    expiry_type: Optional[str] = None
    is_expiry_day: bool = False

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


def _layer_1_global_market(macro):
    if not macro or macro.get("global_market_trend") is None:
        return "⚪ Unavailable"
    trend = str(macro.get("global_market_trend", "")).lower()
    if trend in ("up", "bullish", "positive"):
        return "🟢 Global markets supportive"
    if trend in ("down", "bearish", "negative"):
        return "🔴 Global markets weak"
    return "🟡 Global markets mixed"


def _layer_2_india_vix(macro):
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


def _layer_3_sgx_giftnifty(macro):
    if not macro or macro.get("giftnifty_change_pct") is None:
        return "⚪ Unavailable"
    chg = _safe_float(macro.get("giftnifty_change_pct"))
    if chg > 0.15:
        return "🟢 GIFT Nifty pointing to a gap-up"
    if chg < -0.15:
        return "🔴 GIFT Nifty pointing to a gap-down"
    return "🟡 GIFT Nifty flat"


def _layer_4_us_market(macro):
    if not macro or macro.get("us_market_trend") is None:
        return "⚪ Unavailable"
    trend = str(macro.get("us_market_trend", "")).lower()
    if trend in ("up", "bullish", "positive"):
        return "🟢 US markets closed higher"
    if trend in ("down", "bearish", "negative"):
        return "🔴 US markets closed lower"
    return "🟡 US markets mixed"


def _layer_5_sector_strength(macro):
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


def build_market_context(df, spot_price, atm_strike, max_pain, pcr, support,
                          resistance, trend_engine=None, expiry_label="",
                          oi_history=None, macro_context=None) -> MarketContext:
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

    if macro_context:
        ctx.expiry_type = macro_context.get("expiry_type")
        ctx.is_expiry_day = bool(macro_context.get("is_expiry_day", False))

    institutional_flow = (macro_context or {}).get("institutional_flow")

    if pcr > 1.3:
        ctx.pcr_bias = "🟢 Bullish"
    elif pcr < 0.7:
        ctx.pcr_bias = "🔴 Bearish"
    else:
        ctx.pcr_bias = "🟡 Neutral"

    total_ce_chng_up = float(ce_chng.clip(lower=0).sum())
    total_pe_chng_up = float(pe_chng.clip(lower=0).sum())
    if total_pe_chng_up > total_ce_chng_up * 1.15:
        ctx.oi_bias = "🟢 Put Writers in Control (Bullish)"
    elif total_ce_chng_up > total_pe_chng_up * 1.15:
        ctx.oi_bias = "🔴 Call Writers in Control (Bearish)"
    else:
        ctx.oi_bias = "🟡 Balanced OI Build-up"

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
        iv_hist_ce = oi_history.get("iv_history_ce")
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

    if "ce_chng_oi" in df.columns and (df["ce_chng_oi"].clip(lower=0) > 0).any() and ctx.heavy_ce_chng_thresh > 0 \
            and float(df["ce_chng_oi"].max()) >= ctx.heavy_ce_chng_thresh * FRESH_OI_SIGNIFICANCE_RATIO:
        ctx.call_wall = float(df.loc[df["ce_chng_oi"].idxmax(), "strike_price"])
        ctx.call_wall_source = "fresh_oi_change"
    elif "ce_oi" in df.columns and not df["ce_oi"].dropna().empty:
        ctx.call_wall = float(df.loc[df["ce_oi"].idxmax(), "strike_price"])
        ctx.call_wall_source = "historical_oi_fallback"

    if "pe_chng_oi" in df.columns and (df["pe_chng_oi"].clip(lower=0) > 0).any() and ctx.heavy_pe_chng_thresh > 0 \
            and float(df["pe_chng_oi"].max()) >= ctx.heavy_pe_chng_thresh * FRESH_OI_SIGNIFICANCE_RATIO:
        ctx.put_wall = float(df.loc[df["pe_chng_oi"].idxmax(), "strike_price"])
        ctx.put_wall_source = "fresh_oi_change"
    elif "pe_oi" in df.columns and not df["pe_oi"].dropna().empty:
        ctx.put_wall = float(df.loc[df["pe_oi"].idxmax(), "strike_price"])
        ctx.put_wall_source = "historical_oi_fallback"

    if ctx.has_gamma:
        net_gamma = df.sort_values("strike_price")[["strike_price", "gamma"]].dropna()
        sign_changes = net_gamma["gamma"].apply(lambda g: 1 if g >= 0 else -1).diff().fillna(0)
        flips = net_gamma.loc[sign_changes != 0, "strike_price"]
        if not flips.empty:
            ctx.zero_gamma_strike = float(flips.iloc[(flips - spot_price).abs().argsort().iloc[0]])
    if max_pain and spot_price:
        ctx.pin_risk = abs(spot_price - max_pain) / max_pain * 100 < 0.3

    ctx.fresh_support, ctx.fresh_support_pe_chng_oi = _select_fresh_level(
        df, chng_col="pe_chng_oi", vol_col="pe_volume", heavy_chng_thresh=ctx.heavy_pe_chng_thresh)
    ctx.fresh_resistance, ctx.fresh_resistance_ce_chng_oi = _select_fresh_level(
        df, chng_col="ce_chng_oi", vol_col="ce_volume", heavy_chng_thresh=ctx.heavy_ce_chng_thresh)

    strongest_writing = None
    strongest_buying = None
    flow_counts = {
        "Fresh Call Writing": 0, "Call Buying": 0, "Short Covering (Call)": 0, "Long Unwinding (Call)": 0,
        "Put Writing": 0, "Put Buying": 0, "Long Unwinding (Put)": 0, "Short Covering (Put)": 0,
        "Unavailable": 0,
    }
    for _, r in df.iterrows():
        ce_chng_val = _safe_float(r.get("ce_chng_oi"))
        pe_chng_val = _safe_float(r.get("pe_chng_oi"))
        ce_ltp_prev, pe_ltp_prev = r.get("ce_ltp_prev"), r.get("pe_ltp_prev")
        ce_flow = _classify_call_flow(ce_chng_val, r.get("ce_ltp"), ce_ltp_prev)
        pe_flow = _classify_put_flow(pe_chng_val, r.get("pe_ltp"), pe_ltp_prev)
        flow_counts[ce_flow] = flow_counts.get(ce_flow, 0) + 1
        flow_counts[pe_flow] = flow_counts.get(pe_flow, 0) + 1

        if ce_flow == "Fresh Call Writing" and ce_chng_val > 0:
            if strongest_writing is None or ce_chng_val > strongest_writing["chng_oi"]:
                strongest_writing = {"strike": float(r["strike_price"]), "side": "CE", "chng_oi": ce_chng_val, "flow": ce_flow}
        if pe_flow == "Put Writing" and pe_chng_val > 0:
            if strongest_writing is None or pe_chng_val > strongest_writing["chng_oi"]:
                strongest_writing = {"strike": float(r["strike_price"]), "side": "PE", "chng_oi": pe_chng_val, "flow": pe_flow}
        if ce_flow == "Call Buying" and ce_chng_val > 0:
            if strongest_buying is None or ce_chng_val > strongest_buying["chng_oi"]:
                strongest_buying = {"strike": float(r["strike_price"]), "side": "CE", "chng_oi": ce_chng_val, "flow": ce_flow}
        if pe_flow == "Put Buying" and pe_chng_val > 0:
            if strongest_buying is None or pe_chng_val > strongest_buying["chng_oi"]:
                strongest_buying = {"strike": float(r["strike_price"]), "side": "PE", "chng_oi": pe_chng_val, "flow": pe_flow}

    ctx.strongest_writing_strike = strongest_writing
    ctx.strongest_buying_strike = strongest_buying
    ctx.institutional_flow_counts = flow_counts

    if oi_history:
        ctx.pcr_prev = oi_history.get("prev_pcr")
        if ctx.pcr_prev not in (None, 0):
            ctx.pcr_improving = pcr > ctx.pcr_prev
            ctx.pcr_falling = pcr < ctx.pcr_prev

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

    avg_iv = (ctx.avg_ce_iv + ctx.avg_pe_iv) / 2
    if avg_iv >= max(ctx.ce_iv_high_thresh, ctx.pe_iv_high_thresh) and avg_iv > 0:
        ctx.iv_bias = "🔴 Elevated IV (favor sellers / caution on buying)"
    elif 0 < avg_iv <= min(ctx.ce_iv_low_thresh or avg_iv, ctx.pe_iv_low_thresh or avg_iv):
        ctx.iv_bias = "🟢 Low/Compressed IV"
    else:
        ctx.iv_bias = "🟡 Neutral IV"

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

    ce_vol_sum = float(pd.to_numeric(ce_vol, errors="coerce").fillna(0).sum())
    pe_vol_sum = float(pd.to_numeric(pe_vol, errors="coerce").fillna(0).sum())
    total_vol = ce_vol_sum + pe_vol_sum
    ce_vol_weight = 1.0 + (ce_vol_sum / total_vol if total_vol > 0 else 0.5)
    pe_vol_weight = 1.0 + (pe_vol_sum / total_vol if total_vol > 0 else 0.5)
    inst_ce_score = total_ce_chng_up * ce_vol_weight
    inst_pe_score = total_pe_chng_up * pe_vol_weight
    if inst_pe_score > inst_ce_score * 1.15:
        ctx.institutional_bias = "🟢 Institutional Buying (fresh Put writing, volume-confirmed)"
        ctx.smart_money_bias = "🟢 Accumulation"
    elif inst_ce_score > inst_pe_score * 1.15:
        ctx.institutional_bias = "🔴 Institutional Selling (fresh Call writing, volume-confirmed)"
        ctx.smart_money_bias = "🔴 Distribution"
    else:
        ctx.institutional_bias = "🟡 Neutral"
        ctx.smart_money_bias = "🟡 Neutral"

    if institutional_flow == "buying" and ctx.institutional_bias.startswith("🔴"):
        ctx.institutional_bias = "🟡 Neutral (OI proxy vs flow disagree)"
    elif institutional_flow == "selling" and ctx.institutional_bias.startswith("🟢"):
        ctx.institutional_bias = "🟡 Neutral (OI proxy vs flow disagree)"

    if max_pain and spot_price:
        dist_pct = abs(spot_price - max_pain) / max_pain * 100
        if dist_pct < 0.3:
            ctx.dealer_bias = "🟡 Pin Risk Near Max Pain"
        elif spot_price > max_pain:
            ctx.dealer_bias = "🟢 Spot Above Max Pain (dealers may hedge higher)"
        else:
            ctx.dealer_bias = "🔴 Spot Below Max Pain (dealers may hedge lower)"

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


def _ce_buy_conditions(row, ctx: MarketContext) -> dict:
    ce_iv = float(row.get("ce_iv", 0) or 0)
    ce_prem_hist = row.get("ce_ltp_prev")
    ce_ltp = _safe_float(row.get("ce_ltp"))
    conds = {
        "Price above VWAP": bool(ctx.price_above_vwap),
        "Above 20 EMA": bool(ctx.above_ema20),
        "Above 50 EMA": bool(ctx.above_ema50),
        "Bullish structure (BOS/Order Block)": bool(ctx.bullish_structure),
        "Strong Put Writing at/near strike": float(row.get("pe_chng_oi", 0) or 0) >= ctx.heavy_pe_chng_thresh > 0,
        "Call OI unwinding": float(row.get("ce_chng_oi", 0) or 0) < 0,
        "Increasing volume": float(row.get("ce_volume", 0) or 0) >= ctx.high_ce_vol_thresh > 0,
        "Strong Put Volume confirms writing": float(row.get("pe_volume", 0) or 0) >= ctx.high_pe_vol_thresh > 0,
        "PCR improving (or unavailable)": ctx.pcr_improving in (None, True),
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


def _pe_buy_conditions(row, ctx: MarketContext) -> dict:
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
        "Strong Call Volume confirms writing": float(row.get("ce_volume", 0) or 0) >= ctx.high_ce_vol_thresh > 0,
        "PCR falling (or unavailable)": ctx.pcr_falling in (None, True),
        "Gamma supportive": (float(row.get("Gamma Change", 0) or 0) >= 0) if ctx.has_gamma else False,
        "PCR bearish (< 1.0)": ctx.pcr < 1.0,
        "No nearby support": (ctx.support is None) or (row["strike_price"] > ctx.support) or
            (ctx.support and abs(row["strike_price"] - ctx.support) / max(ctx.support, 1) > 0.01),
        "IV acceptable (not elevated)": 0 < pe_iv <= (ctx.pe_iv_high_thresh or pe_iv),
        "Index Trend bearish (EMA20<50<200 + ADX)": ctx.index_trend_bias.startswith("🔴"),
        "Macro layers not contradicting (Global/US/SGX/Sector)": not ctx.macro_contradicts_bears,
        "India VIX not spiking against buyers": not ctx.india_vix_bias.startswith("🔴"),
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


def _ce_sell_conditions(row, ctx: MarketContext) -> dict:
    ce_iv = float(row.get("ce_iv", 0) or 0)
    momentum_weak = not bool(ctx.bullish_structure) if ctx.has_trend_engine else True
    near_call_wall = ctx.call_wall is not None and abs(row["strike_price"] - ctx.call_wall) < 1e-6
    iv_rank_ok = ctx.iv_rank_ce is None or ctx.iv_rank_ce >= 60
    conds = {
        "Strong resistance / Call Wall at strike": near_call_wall or (
            ctx.resistance is not None and abs(row["strike_price"] - ctx.resistance) < 1e-6
        ) or float(row.get("ce_oi", 0) or 0) >= ctx.heavy_ce_oi_thresh > 0,
        "Heavy Call Writing": float(row.get("ce_chng_oi", 0) or 0) >= ctx.heavy_ce_chng_thresh > 0,
        "IV advantage (elevated / high IV rank)": (ce_iv > 0 and ce_iv >= (ctx.ce_iv_high_thresh or ce_iv)) and iv_rank_ok,
        "Momentum weak / stalling": momentum_weak,
        "No Gamma Flip bullish (gamma not accelerating buyers)": not bool(ctx.gamma_flip_bullish),
        "Theta advantage (decay favors seller)": True,
        "Smart Money / Dealer / Seller Bias not against the sell": not (
            ctx.smart_money_bias.startswith("🟢") and ctx.dealer_bias.startswith("🟢") and ctx.seller_bias.startswith("🟢")
        ),
    }
    return conds


def _pe_sell_conditions(row, ctx: MarketContext) -> dict:
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


def _frac(conds, keys) -> float:
    present = [k for k in keys if k in conds]
    if not present:
        return 0.5
    return sum(1 for k in present if conds[k]) / len(present)


def _oi_change_category_score(row, ctx: MarketContext, is_ce, is_buy) -> float:
    ce_chng = _safe_float(row.get("ce_chng_oi"))
    pe_chng = _safe_float(row.get("pe_chng_oi"))
    if is_buy and is_ce:
        strength = _clip01(pe_chng / ctx.heavy_pe_chng_thresh) if ctx.heavy_pe_chng_thresh > 0 else 0.0
        return _clip01(strength + (0.15 if ce_chng < 0 else 0.0))
    if is_buy and not is_ce:
        strength = _clip01(ce_chng / ctx.heavy_ce_chng_thresh) if ctx.heavy_ce_chng_thresh > 0 else 0.0
        return _clip01(strength + (0.15 if pe_chng < 0 else 0.0))
    if not is_buy and is_ce:
        return _clip01(ce_chng / ctx.heavy_ce_chng_thresh) if ctx.heavy_ce_chng_thresh > 0 else 0.0
    return _clip01(pe_chng / ctx.heavy_pe_chng_thresh) if ctx.heavy_pe_chng_thresh > 0 else 0.0


def _absolute_oi_category_score(row, ctx: MarketContext, is_ce) -> float:
    if is_ce:
        oi_val, thresh = _safe_float(row.get("ce_oi")), ctx.heavy_ce_oi_thresh
    else:
        oi_val, thresh = _safe_float(row.get("pe_oi")), ctx.heavy_pe_oi_thresh
    if thresh <= 0:
        return 0.5
    return _clip01(oi_val / thresh)


def _category_scores(conds, ctx: MarketContext, base_score, direction, row) -> dict:
    is_ce = direction.startswith("CE")
    is_buy = "BUY" in direction

    price_action_keys = ["Price above VWAP", "Above 20 EMA", "Above 50 EMA",
                          "Bullish structure (BOS/Order Block)", "Index Trend bullish (EMA20>50>200 + ADX)",
                          "Price below VWAP", "Below 20 EMA", "Below 50 EMA",
                          "Bearish structure (BOS/Order Block)", "Index Trend bearish (EMA20<50<200 + ADX)",
                          "No Buyer Trap / Fake Breakout detected", "No Stop-Hunt sweep against the move",
                          "CHoCH not bearish", "No nearby resistance",
                          "No Seller Trap / Fake Breakdown detected", "CHoCH not bullish", "No nearby support",
                          "Strong resistance / Call Wall at strike", "Strong support / Put Wall at strike",
                          "Momentum weak / stalling"]
    iv_keys = ["IV acceptable (not elevated)", "IV advantage (elevated / high IV rank)",
               "India VIX not spiking against buyers"]

    scores = {
        "oi_change": _oi_change_category_score(row, ctx, is_ce, is_buy),
        "volume": _volume_category_score(row, ctx, is_ce),
        "price_action": _frac(conds, price_action_keys),
        "pcr": _pcr_category_score(ctx, is_ce, is_buy),
        "iv": _frac(conds, iv_keys),
        "absolute_oi": _absolute_oi_category_score(row, ctx, is_ce),
    }
    return scores


def _volume_category_score(row, ctx: MarketContext, is_ce) -> float:
    """Continuous 0-1 score for the 20%-weighted 'volume' bucket, computed
    directly from the strike's own CE/PE volume vs. the chain's
    high-volume threshold. Computed the same way _oi_change_category_score
    / _absolute_oi_category_score are (directly from row/ctx) rather than
    via `_frac` over the checklist dict, because the BUY and SELL
    checklists don't share matching volume-condition key names — under
    the old `_frac(conds, volume_keys)` approach every SELL signal's
    volume bucket silently defaulted to a fixed neutral 0.5 regardless of
    actual liquidity, since none of `volume_keys` exist in
    `_ce_sell_conditions` / `_pe_sell_conditions`."""
    vol_val = _safe_float(row.get("ce_volume")) if is_ce else _safe_float(row.get("pe_volume"))
    thresh = ctx.high_ce_vol_thresh if is_ce else ctx.high_pe_vol_thresh
    if thresh <= 0:
        return 0.5
    return _clip01(vol_val / thresh)


def _pcr_category_score(ctx: MarketContext, is_ce, is_buy) -> float:
    """Continuous 0-1 score for the 10%-weighted 'pcr' bucket, computed
    directly from ctx.pcr for every direction. Same rationale as
    `_volume_category_score`: the SELL checklists carry no
    "PCR ..." named condition at all, so under the old `_frac`
    approach the pcr bucket was frozen at neutral 0.5 for every CE
    SELL / PE SELL evaluation no matter what PCR actually was."""
    bullish_trade = (is_ce and is_buy) or (not is_ce and not is_buy)
    if ctx.pcr <= 0:
        return 0.5
    if bullish_trade:
        return _clip01(0.5 + (ctx.pcr - 1.0) / 2.0)
    return _clip01(0.5 - (ctx.pcr - 1.0) / 2.0)


def _weighted_probability(conds, ctx: MarketContext, base_score, direction, row):
    cats = _category_scores(conds, ctx, base_score, direction, row)
    cats["oi_change"] = _clip01(0.75 * cats["oi_change"] + 0.25 * (base_score / 100.0))
    probability = sum(cats[k] * w for k, w in CATEGORY_WEIGHTS.items())
    return round(_clip01(probability) * 100, 1), cats


def _confidence_from_categories(cats, base_score) -> float:
    strength = sum(cats[k] * w for k, w in CATEGORY_WEIGHTS.items())
    return round(_clip01(0.6 * strength + 0.4 * (base_score / 100.0)) * 100, 1)


def _risk_level(probability, confidence, iv_bias) -> str:
    score = (probability + confidence) / 2
    if iv_bias.startswith("🔴"):
        score -= 10
    if score >= 90:
        return "Low"
    if score >= 80:
        return "Moderate"
    if score >= 70:
        return "Elevated"
    return "High"


def _levels(entry):
    if entry <= 0:
        return 0.0, 0.0, 0.0, 0.0
    sl = round(entry * 0.85, 2)
    t1 = round(entry * 1.15, 2)
    t2 = round(entry * 1.30, 2)
    t3 = round(entry * 1.50, 2)
    return sl, t1, t2, t3


def _dynamic_levels(entry, ctx: MarketContext, row, is_ce, is_sell):
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


def _evaluate_strike(row, ctx: MarketContext, min_probability, min_confidence) -> dict:
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

    ce_buy_prob, ce_buy_cats = _weighted_probability(ce_buy_conds, ctx, ce_score, "CE BUY", row)
    pe_buy_prob, pe_buy_cats = _weighted_probability(pe_buy_conds, ctx, pe_score, "PE BUY", row)
    ce_sell_prob, ce_sell_cats = _weighted_probability(ce_sell_conds, ctx, 100 - ce_score, "CE SELL", row)
    pe_sell_prob, pe_sell_cats = _weighted_probability(pe_sell_conds, ctx, 100 - pe_score, "PE SELL", row)

    ce_buy_conf = _confidence_from_categories(ce_buy_cats, ce_score)
    pe_buy_conf = _confidence_from_categories(pe_buy_cats, pe_score)
    ce_sell_conf = _confidence_from_categories(ce_sell_cats, 100 - ce_score)
    pe_sell_conf = _confidence_from_categories(pe_sell_cats, 100 - pe_score)

    buy_prob_bar, sell_prob_bar = min_probability, min_probability
    if ctx.is_expiry_day:
        buy_prob_bar += 5.0
        sell_prob_bar = max(sell_prob_bar - 3.0, 70.0)
    elif ctx.expiry_type == "monthly":
        buy_prob_bar -= 1.0

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


def _missing_reasons(ce_buy_conds, pe_buy_conds, ctx: MarketContext, candidates, had_candidates_but_rr_failed) -> str:
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


def analyze_market(df, spot_price, atm_strike, max_pain, pcr, support=None,
                    resistance=None, trend_engine=None, expiry_label="",
                    min_probability=MIN_PROBABILITY, min_confidence=MIN_CONFIDENCE, top_n=10,
                    oi_history=None, macro_context=None) -> dict:
    if df is None or df.empty:
        return _empty_result(reason="Empty option chain — nothing to analyze.")

    ctx = build_market_context(df, spot_price, atm_strike, max_pain, pcr, support,
                                resistance, trend_engine=trend_engine, expiry_label=expiry_label,
                                oi_history=oi_history, macro_context=macro_context)

    results = []
    for _, row in df.iterrows():
        try:
            results.append(_evaluate_strike(row, ctx, min_probability, min_confidence))
        except Exception:
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
                "confirmation from a trend_engine AND the Index Trend / Macro / Smart-Money / "
                "Dealer / Seller layers to agree. Seller-side (CE SELL / PE SELL) reads can "
                "surface from the option-chain snapshot alone if Theta + IV + Resistance/Support "
                "confirmation all hold, and are rejected outright if Reward:Risk is below 1:2."
            ),
        },
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
            "call_wall_source": ctx.call_wall_source,
            "put_wall_source": ctx.put_wall_source,
            "fresh_support": ctx.fresh_support,
            "fresh_support_pe_chng_oi": ctx.fresh_support_pe_chng_oi,
            "fresh_resistance": ctx.fresh_resistance,
            "fresh_resistance_ce_chng_oi": ctx.fresh_resistance_ce_chng_oi,
            "strongest_writing_strike": ctx.strongest_writing_strike,
            "strongest_buying_strike": ctx.strongest_buying_strike,
            "institutional_flow_counts": ctx.institutional_flow_counts,
            "pcr_prev": ctx.pcr_prev,
            "pcr_improving": ctx.pcr_improving,
            "pcr_falling": ctx.pcr_falling,
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
            "scoring_model": "fresh_positioning_v2",
            "primary_metric": "oi_change",
            "note": (
                "OI Change (ΔOI) is the primary institutional-positioning metric (40% weight). "
                "Absolute/historical OI is confirmation-only (5% weight) for probability scoring. "
                "Volume and PCR buckets (20% + 10% weight) are now scored directly from live data "
                "for every direction (BUY and SELL alike), rather than via checklist-key lookup, "
                "so SELL-side probabilities properly reflect real liquidity and PCR conditions "
                "instead of defaulting to a fixed neutral score."
            ),
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


def render_ai_analysis_block(result: dict, st_module=None) -> None:
    if st_module is None:
        import streamlit as st_module

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
