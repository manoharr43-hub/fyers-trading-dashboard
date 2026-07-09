"""
ai_analysis_engine.py
======================
Implements analyze_market(), the module the dashboard imports as:

    from ai_analysis_engine import analyze_market

This module exists to satisfy the "THINK LIKE AN OPTION SELLER & OPTION
BUYER" decision framework: before any BUY/SELL signal is produced, it
walks through Seller Bias -> Buyer Bias -> Market Maker Bias -> Smart
Money Bias -> strict ALL-conditions-true CE BUY / PE BUY / CE SELL /
PE SELL checks -> and only then returns a signal. Missing / unavailable
confirmations fail safe toward WAIT — this engine can only ever be
pushed toward caution by missing data, never forced into a BUY/SELL.

INPUT CONTRACT
--------------
analyze_market() is designed to run AFTER the dashboard's own pipeline
has already computed its per-strike columns, i.e. after:

    df = compute_big_move_scores(df, spot_price, max_pain, pcr, atm_strike)
    df = compute_ai_engine(df, spot_price, atm_strike, max_pain, pcr)

so it can reuse CE Score / PE Score / Breakout Probability / Breakdown
Probability / Institutional Score / Smart Money Score / CE Build-up /
PE Build-up rather than recomputing them. Gamma columns ("gamma",
"Gamma Change") are optional — if absent, the Gamma confirmation simply
does not count toward either side.

An optional `trend_engine` dict — in the shape produced by this
dashboard's compute_scalping_trend_engine() (VWAP, EMA9/20/50, RSI,
ADX, Supertrend direction, Order Block, BOS, volume spike, etc.) — can
be passed in to unlock the underlying-price-action confirmations
(VWAP/EMA/Structure/Volume Expansion) required by STEP 5 and STEP 6
below. Without it, those checks fail safe to False (not True) — per
STEP 9, that alone is enough to keep the result at WAIT, since CE BUY /
PE BUY require ALL listed conditions, not a majority.

This is a heuristic, snapshot-based read-through of the option chain
and (optionally) recent underlying candles. It is not financial advice,
it does not have access to real order-flow/dealer-position data, and
several "Smart Money" / "Dealer" concepts below are necessarily proxied
from OI/Volume/ΔOI/Gamma rather than measured directly. Always confirm
with live price action and manage your own risk.
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

# Percentile-based "heavy" thresholds — relative to the current chain
# snapshot, since only a single point-in-time payload is available.
HEAVY_OI_PCT = 0.80
HEAVY_CHNG_OI_PCT = 0.80
HIGH_VOLUME_PCT = 0.75
IV_HIGH_PCT = 0.75
IV_LOW_PCT = 0.25

RISK_LEVELS = ["Low", "Moderate", "Elevated", "High"]


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


# ══════════════════════════════════════════════════════════════════════════
# Bias primitives (STEP 1-4, "Final Output Format" bias fields)
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class MarketContext:
    """Chain-wide context computed once per analyze_market() call and
    shared by every per-strike evaluation, so every strike is judged
    against the same seller/buyer/dealer/smart-money read."""

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
    above_ema20: Optional[bool] = None
    above_ema50: Optional[bool] = None
    below_ema20: Optional[bool] = None
    below_ema50: Optional[bool] = None
    bullish_structure: Optional[bool] = None
    bearish_structure: Optional[bool] = None
    volume_expansion: Optional[bool] = None
    adx_strong: Optional[bool] = None
    supertrend_dir: Optional[int] = None  # 1 bullish, -1 bearish

    # Bias summary (STEP 1-4 / final output)
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
    expected_direction: str = "🟡 Range-bound"

    has_trend_engine: bool = False
    has_gamma: bool = False


def build_market_context(df: pd.DataFrame, spot_price: float, atm_strike: float,
                          max_pain: float, pcr: float, support: Optional[float],
                          resistance: Optional[float], trend_engine: Optional[dict] = None,
                          expiry_label: str = "") -> MarketContext:
    """STEP 1-4: derive every chain-wide bias BEFORE any per-strike
    BUY/SELL condition is evaluated. Nothing here looks at a single
    strike in isolation — it is deliberately the full market context."""
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

    # ── Seller Bias (STEP 1) — sellers are assumed in control unless
    # buyers are clearly overwhelming them with fresh volume + ΔOI ─────
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

    # ── Institutional / Smart Money / Dealer Bias (OI+ΔOI proxies) ──────
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

    # ── Trend-engine derived checks (VWAP/EMA/Structure/Volume) ─────────
    if trend_engine and trend_engine.get("available"):
        ctx.has_trend_engine = True
        last_close = trend_engine.get("last_close")
        vwap = trend_engine.get("vwap")
        ema20, ema50 = trend_engine.get("ema20"), trend_engine.get("ema50")
        adx = trend_engine.get("adx", 0)
        buy_checks = trend_engine.get("buy_checks", {})
        sell_checks = trend_engine.get("sell_checks", {})

        if last_close is not None and vwap is not None:
            ctx.price_above_vwap = last_close > vwap
            ctx.price_below_vwap = last_close < vwap
        if last_close is not None and ema20 is not None:
            ctx.above_ema20 = last_close > ema20
            ctx.below_ema20 = last_close < ema20
        if last_close is not None and ema50 is not None:
            ctx.above_ema50 = last_close > ema50
            ctx.below_ema50 = last_close < ema50

        ctx.bullish_structure = bool(buy_checks.get("Bullish BOS")) or bool(buy_checks.get("Bullish Order Block"))
        ctx.bearish_structure = bool(sell_checks.get("Bearish BOS")) or bool(sell_checks.get("Bearish Order Block"))
        ctx.volume_expansion = bool(buy_checks.get("Volume Spike") or sell_checks.get("Volume Spike"))
        ctx.adx_strong = adx > 25
        ctx.supertrend_dir = trend_engine.get("supertrend_dir")

    # ── Buyer Bias (STEP 2) — requires trend engine for real momentum
    # confirmation; without it, buyer bias stays "Unconfirmed" (weak),
    # per STEP 9's fail-safe-to-WAIT rule ────────────────────────────────
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

    # ── Overall Market Bias / Expected Direction (rollup) ────────────────
    bull_signals = sum([
        ctx.pcr_bias.startswith("🟢"), ctx.oi_bias.startswith("🟢"),
        ctx.institutional_bias.startswith("🟢"), ctx.gamma_bias.startswith("🟢"),
        ctx.buyer_bias.startswith("🟢"),
    ])
    bear_signals = sum([
        ctx.pcr_bias.startswith("🔴"), ctx.oi_bias.startswith("🔴"),
        ctx.institutional_bias.startswith("🔴"), ctx.gamma_bias.startswith("🔴"),
        ctx.buyer_bias.startswith("🔴"),
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
    """STEP 5 — every condition must be True for a CE BUY signal."""
    ce_iv = float(row.get("ce_iv", 0) or 0)
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
        "No nearby resistance": (ctx.resistance is None) or (row["strike_price"] < ctx.resistance) or \
            (ctx.resistance and abs(row["strike_price"] - ctx.resistance) / max(ctx.resistance, 1) > 0.01),
    }
    return conds


def _pe_buy_conditions(row: pd.Series, ctx: MarketContext) -> dict:
    """STEP 6 — every condition must be True for a PE BUY signal."""
    pe_iv = float(row.get("pe_iv", 0) or 0)
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
        "No nearby support": (ctx.support is None) or (row["strike_price"] > ctx.support) or \
            (ctx.support and abs(row["strike_price"] - ctx.support) / max(ctx.support, 1) > 0.01),
        "IV acceptable (not elevated)": 0 < pe_iv <= (ctx.pe_iv_high_thresh or pe_iv),
    }
    return conds


def _ce_sell_conditions(row: pd.Series, ctx: MarketContext) -> dict:
    """STEP 7 — Call SELL: strong resistance + overpriced premium + weak
    momentum + time-decay edge."""
    ce_iv = float(row.get("ce_iv", 0) or 0)
    momentum_weak = not bool(ctx.bullish_structure) if ctx.has_trend_engine else True
    conds = {
        "Strong resistance at strike": ctx.resistance is not None and abs(row["strike_price"] - ctx.resistance) < 1e-6
            or float(row.get("ce_oi", 0) or 0) >= ctx.heavy_ce_oi_thresh > 0,
        "Heavy Call Writing": float(row.get("ce_chng_oi", 0) or 0) >= ctx.heavy_ce_chng_thresh > 0,
        "Premium/IV elevated": ce_iv > 0 and ce_iv >= (ctx.ce_iv_high_thresh or ce_iv),
        "Momentum weak / stalling": momentum_weak,
        "Time decay favors seller (theta edge)": True,  # structural — premium decays every session, always true for a seller once the above hold
    }
    return conds


def _pe_sell_conditions(row: pd.Series, ctx: MarketContext) -> dict:
    """STEP 7 — Put SELL: strong support + overpriced premium + weak
    momentum + time-decay edge."""
    pe_iv = float(row.get("pe_iv", 0) or 0)
    momentum_weak = not bool(ctx.bearish_structure) if ctx.has_trend_engine else True
    conds = {
        "Strong support at strike": ctx.support is not None and abs(row["strike_price"] - ctx.support) < 1e-6
            or float(row.get("pe_oi", 0) or 0) >= ctx.heavy_pe_oi_thresh > 0,
        "Heavy Put Writing": float(row.get("pe_chng_oi", 0) or 0) >= ctx.heavy_pe_chng_thresh > 0,
        "Premium/IV elevated": pe_iv > 0 and pe_iv >= (ctx.pe_iv_high_thresh or pe_iv),
        "Momentum weak / stalling": momentum_weak,
        "Time decay favors seller (theta edge)": True,
    }
    return conds


# ══════════════════════════════════════════════════════════════════════════
# Probability / Confidence / Risk scoring
# ══════════════════════════════════════════════════════════════════════════

def _probability_from_conditions(conds: dict, base_score: float) -> float:
    """Probability blends the strike's own model score (CE/PE Score,
    already 0-100 from the dashboard's AI Engine) with the fraction of
    the strict checklist that passed — so a strike only reaches the
    required >85% probability bar when BOTH the quantitative score and
    the qualitative condition list agree."""
    if not conds:
        return 0.0
    passed_frac = sum(1 for v in conds.values() if v) / len(conds)
    return round(_clip01(0.5 * (base_score / 100.0) + 0.5 * passed_frac) * 100, 1)


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
    if entry <= 0:
        return 0.0, 0.0, 0.0, 0.0
    sl = round(entry * 0.85, 2)
    t1 = round(entry * 1.15, 2)
    t2 = round(entry * 1.30, 2)
    t3 = round(entry * 1.50, 2)
    return sl, t1, t2, t3


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

    ce_buy_prob = _probability_from_conditions(ce_buy_conds, ce_score)
    pe_buy_prob = _probability_from_conditions(pe_buy_conds, pe_score)
    ce_sell_prob = _probability_from_conditions(ce_sell_conds, 100 - ce_score)
    pe_sell_prob = _probability_from_conditions(pe_sell_conds, 100 - pe_score)

    ce_buy_conf = round((ce_score + ce_buy_prob) / 2, 1)
    pe_buy_conf = round((pe_score + pe_buy_prob) / 2, 1)
    ce_sell_conf = round(((100 - ce_score) + ce_sell_prob) / 2, 1)
    pe_sell_conf = round(((100 - pe_score) + pe_sell_prob) / 2, 1)

    # STEP 8 — avoid-trap gate: never allow a BUY through if it fights
    # the market-wide institutional/smart-money bias, even if the local
    # strike checklist happened to pass.
    if ce_buy_all and ctx.institutional_bias.startswith("🔴") and ctx.smart_money_bias.startswith("🔴"):
        ce_buy_all = False
    if pe_buy_all and ctx.institutional_bias.startswith("🟢") and ctx.smart_money_bias.startswith("🟢"):
        pe_buy_all = False

    candidates = []
    if ce_buy_all and ce_buy_prob >= min_probability and ce_buy_conf >= min_confidence:
        candidates.append(("CE BUY", ce_buy_prob, ce_buy_conf, ce_buy_conds, row.get("ce_ltp", 0)))
    if pe_buy_all and pe_buy_prob >= min_probability and pe_buy_conf >= min_confidence:
        candidates.append(("PE BUY", pe_buy_prob, pe_buy_conf, pe_buy_conds, row.get("pe_ltp", 0)))
    if ce_sell_all and ce_sell_prob >= min_probability:
        candidates.append(("CE SELL", ce_sell_prob, ce_sell_conf, ce_sell_conds, row.get("ce_ltp", 0)))
    if pe_sell_all and pe_sell_prob >= min_probability:
        candidates.append(("PE SELL", pe_sell_prob, pe_sell_conf, pe_sell_conds, row.get("pe_ltp", 0)))

    if not candidates:
        return {
            "Strike": strike, "Recommended Action": "WAIT",
            "Probability %": max(ce_buy_prob, pe_buy_prob, ce_sell_prob, pe_sell_prob),
            "Confidence %": max(ce_buy_conf, pe_buy_conf, ce_sell_conf, pe_sell_conf),
            "Risk Level": "—", "Entry": 0.0, "Stop Loss": 0.0,
            "Target 1": 0.0, "Target 2": 0.0, "Target 3": 0.0,
            "Reason for Trade": "—",
            "Reason to Avoid Trade": _missing_reasons(ce_buy_conds, pe_buy_conds, ctx),
            "Invalidation Level": "—",
            "_ce_buy_conditions": ce_buy_conds, "_pe_buy_conditions": pe_buy_conds,
            "_ce_sell_conditions": ce_sell_conds, "_pe_sell_conditions": pe_sell_conds,
        }

    action, prob, conf, conds, ltp = max(candidates, key=lambda c: (c[1] + c[2]))
    ltp = float(ltp or 0)
    is_sell = action.endswith("SELL")

    if is_sell:
        # Premium SELL: SL/targets are inverted vs a BUY (credit position).
        entry = ltp
        sl = round(entry * 1.20, 2)   # premium expanding 20% against the seller
        t1 = round(entry * 0.85, 2)
        t2 = round(entry * 0.70, 2)
        t3 = round(entry * 0.50, 2)
    else:
        entry = ltp
        sl, t1, t2, t3 = _levels(entry)

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
        "Reason for Trade": " · ".join(k for k, v in conds.items() if v),
        "Reason to Avoid Trade": _missing_reasons(
            _ce_buy_conditions(row, ctx) if action != "CE BUY" else {},
            _pe_buy_conditions(row, ctx) if action != "PE BUY" else {}, ctx),
        "Invalidation Level": f"{invalidation:,.0f}" if invalidation else "—",
        "_ce_buy_conditions": ce_buy_conds, "_pe_buy_conditions": pe_buy_conds,
        "_ce_sell_conditions": ce_sell_conds, "_pe_sell_conditions": pe_sell_conds,
    }


def _missing_reasons(ce_buy_conds: dict, pe_buy_conds: dict, ctx: MarketContext) -> str:
    missing = []
    if not ctx.has_trend_engine:
        missing.append("No underlying candle data (VWAP/EMA/Structure unconfirmed)")
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
                    min_confidence: float = MIN_CONFIDENCE, top_n: int = 10) -> dict:
    """
    Main entry point. Runs the full seller -> buyer -> market-maker ->
    smart-money -> strict-condition decision framework and returns a
    dict matching the "FINAL OUTPUT FORMAT" section of the framework:

        {
          "market_bias", "smart_money_bias", "seller_bias", "buyer_bias",
          "institutional_bias", "dealer_bias", "pcr_bias", "oi_bias",
          "gamma_bias", "iv_bias", "expected_direction",
          "best_trade": {...} | None,
          "top_trades": [ {...}, ... ],   # up to top_n qualifying trades
          "all_strikes": [ {...}, ... ],  # every strike, mostly WAIT
          "data_quality": {...},
        }

    `df` must already carry the dashboard's own CE Score / PE Score
    columns (i.e. call this AFTER compute_ai_engine()). If those columns
    are missing, this degrades gracefully — every strike simply scores
    lower on the probability blend and is far less likely to clear the
    minimum bar, which is the correct fail-safe direction (STEP 9).
    """
    if df is None or df.empty:
        return _empty_result(reason="Empty option chain — nothing to analyze.")

    ctx = build_market_context(df, spot_price, atm_strike, max_pain, pcr, support,
                                resistance, trend_engine=trend_engine, expiry_label=expiry_label)

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
                "output). Without it, buyer-side signals correctly stay at WAIT per the framework's "
                "'missing confirmation -> WAIT' rule; seller-side (CE SELL / PE SELL) reads can still "
                "surface from the option-chain snapshot alone."
            ),
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

    st_module.markdown('<div class="block-title">🧠 AI Market Analysis (Seller → Buyer → Dealer → Smart Money)</div>',
                        unsafe_allow_html=True)

    bias_fields = [
        ("Market Bias", result["market_bias"]), ("Expected Direction", result["expected_direction"]),
        ("Seller Bias", result["seller_bias"]), ("Buyer Bias", result["buyer_bias"]),
        ("Smart Money Bias", result["smart_money_bias"]), ("Institutional Bias", result["institutional_bias"]),
        ("Dealer Bias", result["dealer_bias"]), ("PCR Bias", result["pcr_bias"]),
        ("OI Bias", result["oi_bias"]), ("Gamma Bias", result["gamma_bias"]), ("IV Bias", result["iv_bias"]),
    ]
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
            "🟡 WAIT — no strike currently satisfies every condition in the Seller/Buyer/Dealer/"
            "Smart-Money checklist at the required probability/confidence bar. " +
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
