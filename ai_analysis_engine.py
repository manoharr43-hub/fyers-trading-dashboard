"""
AI_ANALYSIS_ENGINE.py
=====================
Institutional Quantitative Trading AI — NSE Market (India)
Designed for FYERS live market data integration.

Rules:
  - Never generate fake data.
  - Never invent prices, OI, futures, or greeks.
  - If critical data is missing → Recommendation = WAIT.
  - Both option_chain.py and ai_market_intelligence.py must import and use this engine.
  - Both modules must always return the same AI direction.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum


# ==============================================================================
# ENUMS
# ==============================================================================

class Signal(str, Enum):
    BUY  = "BUY"
    SELL = "SELL"
    WAIT = "WAIT"

class Trend(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"

class FuturesClass(str, Enum):
    LONG_BUILDUP   = "Long Build-up"
    SHORT_BUILDUP  = "Short Build-up"
    LONG_UNWIND    = "Long Unwinding"
    SHORT_COVER    = "Short Covering"
    UNKNOWN        = "Unknown"

class OptionFlow(str, Enum):
    PUT_WRITING    = "Put Writing"
    CALL_WRITING   = "Call Writing"
    PUT_UNWIND     = "Put Unwinding"
    CALL_UNWIND    = "Call Unwinding"
    NEUTRAL        = "Neutral"

class NewsSentiment(str, Enum):
    BULLISH = "Bullish"
    BEARISH = "Bearish"
    NEUTRAL = "Neutral"


# ==============================================================================
# INPUT DATA CLASSES
# ==============================================================================

@dataclass
class SpotData:
    """STEP 1 — Spot Market Data"""
    price:       Optional[float] = None
    open:        Optional[float] = None
    high:        Optional[float] = None
    low:         Optional[float] = None
    close:       Optional[float] = None
    vwap:        Optional[float] = None
    ema20:       Optional[float] = None
    ema50:       Optional[float] = None
    ema200:      Optional[float] = None
    rsi:         Optional[float] = None   # 0–100
    macd:        Optional[float] = None
    macd_signal: Optional[float] = None
    adx:         Optional[float] = None
    atr:         Optional[float] = None
    supertrend:  Optional[float] = None
    supertrend_direction: Optional[str] = None  # "UP" or "DOWN"


@dataclass
class FuturesData:
    """STEP 2 — Futures Market Data"""
    futures_price:  Optional[float] = None
    spot_price:     Optional[float] = None
    open_interest:  Optional[float] = None
    oi_change:      Optional[float] = None
    volume:         Optional[float] = None
    avg_volume:     Optional[float] = None


@dataclass
class OptionChainData:
    """STEP 3 — Option Chain Data"""
    pcr:               Optional[float] = None   # Put-Call Ratio
    max_pain:          Optional[float] = None
    total_call_oi:     Optional[float] = None
    total_put_oi:      Optional[float] = None
    call_oi_change:    Optional[float] = None
    put_oi_change:     Optional[float] = None
    ce_volume:         Optional[float] = None
    pe_volume:         Optional[float] = None
    iv:                Optional[float] = None
    delta:             Optional[float] = None
    gamma:             Optional[float] = None
    theta:             Optional[float] = None
    vega:              Optional[float] = None
    gamma_exposure:    Optional[float] = None
    delta_exposure:    Optional[float] = None


@dataclass
class SmartMoneyData:
    """STEP 4 — Smart Money / Price Action Data"""
    bos:              Optional[bool]  = None   # Break of Structure
    choch:            Optional[bool]  = None   # Change of Character
    order_block:      Optional[str]   = None   # "BULLISH" / "BEARISH" / None
    breaker_block:    Optional[str]   = None
    mitigation_block: Optional[str]   = None
    fair_value_gap:   Optional[str]   = None   # "BULLISH" / "BEARISH" / None
    liquidity_sweep:  Optional[str]   = None   # "HIGH" / "LOW" / None
    equal_high:       Optional[bool]  = None
    equal_low:        Optional[bool]  = None
    premium_zone:     Optional[bool]  = None
    discount_zone:    Optional[bool]  = None


@dataclass
class InstitutionalData:
    """STEP 5 — FII / DII Flow"""
    fii_buy:  Optional[float] = None
    fii_sell: Optional[float] = None
    dii_buy:  Optional[float] = None
    dii_sell: Optional[float] = None

    @property
    def net_fii(self) -> Optional[float]:
        if self.fii_buy is None or self.fii_sell is None:
            return None
        return self.fii_buy - self.fii_sell

    @property
    def net_dii(self) -> Optional[float]:
        if self.dii_buy is None or self.dii_sell is None:
            return None
        return self.dii_buy - self.dii_sell


@dataclass
class GlobalData:
    """STEP 6 — Global Market Context"""
    india_vix:    Optional[float] = None
    gift_nifty:   Optional[float] = None
    usdinr:       Optional[float] = None
    crude_oil:    Optional[float] = None
    gold:         Optional[float] = None
    us_market:    Optional[str]   = None   # "BULLISH" / "BEARISH" / "NEUTRAL"
    asian_market: Optional[str]   = None


@dataclass
class NewsData:
    """STEP 7 — News & Sentiment"""
    headline:   Optional[str] = None
    sentiment:  Optional[NewsSentiment] = None
    impact:     Optional[str] = None   # "HIGH" / "MEDIUM" / "LOW"


@dataclass
class MarketBreadthData:
    """STEP 8 — Market Breadth"""
    advance:        Optional[int]   = None
    decline:        Optional[int]   = None
    sector_strength: Optional[str]  = None
    top_gainers:    Optional[list]  = field(default_factory=list)
    top_losers:     Optional[list]  = field(default_factory=list)
    volume_leaders: Optional[list]  = field(default_factory=list)


# ==============================================================================
# OUTPUT DATA CLASSES
# ==============================================================================

@dataclass
class AIScore:
    """STEP 10 — Per-dimension scores (0–10 scale)"""
    spot:         float = 0.0
    future:       float = 0.0
    option_chain: float = 0.0
    smart_money:  float = 0.0
    institution:  float = 0.0
    news:         float = 0.0
    volume:       float = 0.0
    momentum:     float = 0.0
    trend:        float = 0.0

    @property
    def overall(self) -> float:
        scores = [
            self.spot, self.future, self.option_chain,
            self.smart_money, self.institution, self.news,
            self.volume, self.momentum, self.trend,
        ]
        valid = [s for s in scores if s is not None]
        return round(sum(valid) / len(valid), 2) if valid else 0.0

    @property
    def confidence_pct(self) -> float:
        return round(self.overall * 10, 1)


@dataclass
class RiskManagement:
    """STEP 11 — Risk Parameters"""
    entry:         Optional[float] = None
    stop_loss:     Optional[float] = None
    target_1:      Optional[float] = None
    target_2:      Optional[float] = None
    target_3:      Optional[float] = None
    rr_ratio:      Optional[float] = None
    atr_stop_loss: Optional[float] = None


@dataclass
class AnalysisResult:
    """STEP 12 — Final Output"""
    # Direction probabilities
    bullish_pct:  float = 0.0
    bearish_pct:  float = 0.0
    neutral_pct:  float = 0.0

    # Trend signals per layer
    spot_trend:        Trend = Trend.NEUTRAL
    future_trend:      Trend = Trend.NEUTRAL
    option_chain_trend: Trend = Trend.NEUTRAL
    institutional_flow: Trend = Trend.NEUTRAL
    smart_money_dir:   Trend = Trend.NEUTRAL

    # Risk parameters
    entry:     Optional[float] = None
    stop_loss: Optional[float] = None
    target_1:  Optional[float] = None
    target_2:  Optional[float] = None
    target_3:  Optional[float] = None

    # AI Score
    score:      AIScore = field(default_factory=AIScore)
    confidence: float = 0.0

    # Final signal
    recommendation: Signal = Signal.WAIT
    reason:         str    = "Insufficient Market Confirmation"

    # Futures classification
    futures_class: FuturesClass = FuturesClass.UNKNOWN
    option_flow:   OptionFlow   = OptionFlow.NEUTRAL

    # Premium/discount
    futures_premium_discount: Optional[float] = None


# ==============================================================================
# ANALYSIS ENGINE
# ==============================================================================

class AIAnalysisEngine:
    """
    Institutional Quantitative Trading AI — NSE Market
    ---------------------------------------------------
    Pass all available live data.  Any missing critical field
    will automatically produce Recommendation = WAIT.
    """

    # PCR thresholds
    PCR_BULLISH_MIN = 1.0
    PCR_BEARISH_MAX = 0.8

    # ADX trend strength
    ADX_TREND_MIN = 20.0

    # RSI thresholds
    RSI_OVERBOUGHT = 70
    RSI_OVERSOLD   = 30

    def __init__(
        self,
        spot:      SpotData,
        futures:   FuturesData,
        option:    OptionChainData,
        smart:     SmartMoneyData,
        inst:      InstitutionalData,
        global_:   GlobalData,
        news:      NewsData,
        breadth:   MarketBreadthData,
    ):
        self.spot    = spot
        self.futures = futures
        self.option  = option
        self.smart   = smart
        self.inst    = inst
        self.global_ = global_
        self.news    = news
        self.breadth = breadth

    # ------------------------------------------------------------------
    # STEP 1 — Spot Analysis
    # ------------------------------------------------------------------
    def _analyse_spot(self) -> tuple[Trend, float]:
        s = self.spot
        if s.price is None:
            return Trend.NEUTRAL, 0.0

        bull = 0
        bear = 0
        total = 0

        # Price vs EMAs
        for ema in [s.ema20, s.ema50, s.ema200]:
            if ema is not None:
                total += 1
                if s.price > ema:
                    bull += 1
                else:
                    bear += 1

        # Price vs VWAP
        if s.vwap is not None:
            total += 1
            if s.price > s.vwap:
                bull += 1
            else:
                bear += 1

        # RSI
        if s.rsi is not None:
            total += 1
            if s.rsi > 55:
                bull += 1
            elif s.rsi < 45:
                bear += 1

        # MACD
        if s.macd is not None and s.macd_signal is not None:
            total += 1
            if s.macd > s.macd_signal:
                bull += 1
            else:
                bear += 1

        # SuperTrend
        if s.supertrend_direction is not None:
            total += 1
            if s.supertrend_direction.upper() == "UP":
                bull += 1
            else:
                bear += 1

        if total == 0:
            return Trend.NEUTRAL, 0.0

        score = round((bull / total) * 10, 2)
        if bull > bear:
            return Trend.BULLISH, score
        elif bear > bull:
            return Trend.BEARISH, round((bear / total) * 10, 2)
        return Trend.NEUTRAL, 5.0

    # ------------------------------------------------------------------
    # STEP 2 — Futures Analysis
    # ------------------------------------------------------------------
    def _analyse_futures(self) -> tuple[Trend, FuturesClass, Optional[float], float]:
        f = self.futures
        if f.futures_price is None or f.spot_price is None:
            return Trend.NEUTRAL, FuturesClass.UNKNOWN, None, 0.0

        premium = round(f.futures_price - f.spot_price, 2)

        cls = FuturesClass.UNKNOWN
        score = 5.0

        if f.oi_change is not None and f.volume is not None and f.avg_volume is not None:
            price_up   = f.futures_price >= f.spot_price
            oi_up      = f.oi_change > 0
            vol_high   = f.volume > f.avg_volume

            if price_up and oi_up:
                cls   = FuturesClass.LONG_BUILDUP
                score = 8.0 if vol_high else 6.5
            elif not price_up and oi_up:
                cls   = FuturesClass.SHORT_BUILDUP
                score = 2.0 if vol_high else 3.5
            elif price_up and not oi_up:
                cls   = FuturesClass.SHORT_COVER
                score = 6.0
            elif not price_up and not oi_up:
                cls   = FuturesClass.LONG_UNWIND
                score = 4.0

        if cls in (FuturesClass.LONG_BUILDUP, FuturesClass.SHORT_COVER):
            trend = Trend.BULLISH
        elif cls in (FuturesClass.SHORT_BUILDUP, FuturesClass.LONG_UNWIND):
            trend = Trend.BEARISH
        else:
            trend = Trend.NEUTRAL

        return trend, cls, premium, round(score, 2)

    # ------------------------------------------------------------------
    # STEP 3 — Option Chain Analysis
    # ------------------------------------------------------------------
    def _analyse_options(self) -> tuple[Trend, OptionFlow, float]:
        o = self.option
        if o.pcr is None:
            return Trend.NEUTRAL, OptionFlow.NEUTRAL, 0.0

        # PCR-based trend
        if o.pcr >= self.PCR_BULLISH_MIN:
            trend = Trend.BULLISH
        elif o.pcr <= self.PCR_BEARISH_MAX:
            trend = Trend.BEARISH
        else:
            trend = Trend.NEUTRAL

        # Detect option flow
        flow = OptionFlow.NEUTRAL
        if o.put_oi_change is not None and o.call_oi_change is not None:
            if o.put_oi_change > 0 and o.put_oi_change > o.call_oi_change:
                flow = OptionFlow.PUT_WRITING
            elif o.call_oi_change > 0 and o.call_oi_change > o.put_oi_change:
                flow = OptionFlow.CALL_WRITING
            elif o.put_oi_change < 0:
                flow = OptionFlow.PUT_UNWIND
            elif o.call_oi_change < 0:
                flow = OptionFlow.CALL_UNWIND

        # Score
        if trend == Trend.BULLISH:
            score = min(10.0, 5.0 + (o.pcr - 1.0) * 5)
        elif trend == Trend.BEARISH:
            score = max(0.0, 5.0 - (1.0 - o.pcr) * 5)
        else:
            score = 5.0

        return trend, flow, round(score, 2)

    # ------------------------------------------------------------------
    # STEP 4 — Smart Money Analysis
    # ------------------------------------------------------------------
    def _analyse_smart_money(self) -> tuple[Trend, float]:
        sm = self.smart
        bull = 0
        bear = 0
        total = 0

        if sm.bos is not None:
            total += 1
            if sm.bos:
                bull += 1

        if sm.choch is not None:
            total += 1
            if sm.choch:
                bear += 1   # CHOCH implies potential reversal (bearish context)

        if sm.order_block:
            total += 1
            if sm.order_block.upper() == "BULLISH":
                bull += 1
            elif sm.order_block.upper() == "BEARISH":
                bear += 1

        if sm.fair_value_gap:
            total += 1
            if sm.fair_value_gap.upper() == "BULLISH":
                bull += 1
            elif sm.fair_value_gap.upper() == "BEARISH":
                bear += 1

        if sm.liquidity_sweep:
            total += 1
            if sm.liquidity_sweep.upper() == "LOW":
                bull += 1   # Sweep of lows → reversal up
            elif sm.liquidity_sweep.upper() == "HIGH":
                bear += 1

        if sm.discount_zone is True:
            total += 1
            bull += 1
        if sm.premium_zone is True:
            total += 1
            bear += 1

        if total == 0:
            return Trend.NEUTRAL, 5.0

        if bull > bear:
            return Trend.BULLISH, round((bull / total) * 10, 2)
        elif bear > bull:
            return Trend.BEARISH, round((bear / total) * 10, 2)
        return Trend.NEUTRAL, 5.0

    # ------------------------------------------------------------------
    # STEP 5 — Institutional Flow
    # ------------------------------------------------------------------
    def _analyse_institution(self) -> tuple[Trend, float]:
        net = self.inst.net_fii
        if net is None:
            return Trend.NEUTRAL, 5.0
        if net > 0:
            score = min(10.0, 5.0 + net / 1000)
            return Trend.BULLISH, round(score, 2)
        elif net < 0:
            score = max(0.0, 5.0 + net / 1000)
            return Trend.BEARISH, round(score, 2)
        return Trend.NEUTRAL, 5.0

    # ------------------------------------------------------------------
    # STEP 6 — Global Market
    # ------------------------------------------------------------------
    def _analyse_global(self) -> float:
        g = self.global_
        bull = 0
        bear = 0
        total = 0

        if g.india_vix is not None:
            total += 1
            if g.india_vix < 15:
                bull += 1
            elif g.india_vix > 20:
                bear += 1

        for mkt in [g.us_market, g.asian_market]:
            if mkt is not None:
                total += 1
                if mkt.upper() == "BULLISH":
                    bull += 1
                elif mkt.upper() == "BEARISH":
                    bear += 1

        if total == 0:
            return 5.0
        return round(((bull - bear + total) / (2 * total)) * 10, 2)

    # ------------------------------------------------------------------
    # STEP 7 — News Sentiment
    # ------------------------------------------------------------------
    def _analyse_news(self) -> tuple[NewsSentiment, float]:
        if self.news.sentiment is None:
            return NewsSentiment.NEUTRAL, 5.0
        if self.news.sentiment == NewsSentiment.BULLISH:
            return NewsSentiment.BULLISH, 8.0
        elif self.news.sentiment == NewsSentiment.BEARISH:
            return NewsSentiment.BEARISH, 2.0
        return NewsSentiment.NEUTRAL, 5.0

    # ------------------------------------------------------------------
    # STEP 8 — Market Breadth (Volume / Breadth score)
    # ------------------------------------------------------------------
    def _analyse_breadth(self) -> float:
        b = self.breadth
        if b.advance is None or b.decline is None:
            return 5.0
        total = b.advance + b.decline
        if total == 0:
            return 5.0
        return round((b.advance / total) * 10, 2)

    # ------------------------------------------------------------------
    # STEP 9 — AI Confirmation Gate
    # ------------------------------------------------------------------
    def _confirm_signal(
        self,
        spot_trend:  Trend,
        fut_trend:   Trend,
        opt_trend:   Trend,
        opt_flow:    OptionFlow,
        inst_trend:  Trend,
        sm_trend:    Trend,
        news_sent:   NewsSentiment,
        vol_score:   float,
    ) -> tuple[Signal, str]:
        """
        BUY only when ALL conditions are satisfied.
        SELL only when ALL conditions are satisfied.
        Otherwise WAIT.
        """

        # --- BUY conditions ---
        buy_conditions = {
            "Spot Bullish":        spot_trend  == Trend.BULLISH,
            "Future Bullish":      fut_trend   == Trend.BULLISH,
            "Put Writing Active":  opt_flow    == OptionFlow.PUT_WRITING,
            "PCR Bullish":         opt_trend   == Trend.BULLISH,
            "Volume Increasing":   vol_score   >= 6.0,
            "Smart Money Bullish": sm_trend    == Trend.BULLISH,
            "FII Buying":          inst_trend  == Trend.BULLISH,
            "News Bullish":        news_sent   == NewsSentiment.BULLISH,
        }

        # --- SELL conditions ---
        sell_conditions = {
            "Spot Bearish":        spot_trend  == Trend.BEARISH,
            "Future Bearish":      fut_trend   == Trend.BEARISH,
            "Call Writing Active": opt_flow    == OptionFlow.CALL_WRITING,
            "PCR Bearish":         opt_trend   == Trend.BEARISH,
            "Volume Increasing":   vol_score   >= 6.0,
            "Smart Money Bearish": sm_trend    == Trend.BEARISH,
            "FII Selling":         inst_trend  == Trend.BEARISH,
            "News Bearish":        news_sent   == NewsSentiment.BEARISH,
        }

        if all(buy_conditions.values()):
            return Signal.BUY, "All institutional BUY conditions confirmed."

        if all(sell_conditions.values()):
            return Signal.SELL, "All institutional SELL conditions confirmed."

        # Build partial reason
        failed_buy  = [k for k, v in buy_conditions.items()  if not v]
        failed_sell = [k for k, v in sell_conditions.items() if not v]

        if len(failed_buy) <= len(failed_sell):
            missing = ", ".join(failed_buy[:3])
        else:
            missing = ", ".join(failed_sell[:3])

        return Signal.WAIT, f"Insufficient Market Confirmation — missing: {missing}."

    # ------------------------------------------------------------------
    # STEP 11 — Risk Management
    # ------------------------------------------------------------------
    def _compute_risk(self, signal: Signal) -> RiskManagement:
        s   = self.spot
        rm  = RiskManagement()

        if s.price is None or s.atr is None or signal == Signal.WAIT:
            return rm

        atr = s.atr
        rm.entry = s.price

        if signal == Signal.BUY:
            rm.atr_stop_loss = round(s.price - 1.5 * atr, 2)
            rm.stop_loss     = rm.atr_stop_loss
            rm.target_1      = round(s.price + 1.5 * atr, 2)
            rm.target_2      = round(s.price + 3.0 * atr, 2)
            rm.target_3      = round(s.price + 5.0 * atr, 2)
        elif signal == Signal.SELL:
            rm.atr_stop_loss = round(s.price + 1.5 * atr, 2)
            rm.stop_loss     = rm.atr_stop_loss
            rm.target_1      = round(s.price - 1.5 * atr, 2)
            rm.target_2      = round(s.price - 3.0 * atr, 2)
            rm.target_3      = round(s.price - 5.0 * atr, 2)

        risk   = abs(rm.entry - rm.stop_loss) if rm.stop_loss else None
        reward = abs(rm.target_1 - rm.entry) if rm.target_1 else None
        if risk and reward and risk != 0:
            rm.rr_ratio = round(reward / risk, 2)

        return rm

    # ------------------------------------------------------------------
    # MASTER run()
    # ------------------------------------------------------------------
    def run(self) -> AnalysisResult:
        # --- Run all steps ---
        spot_trend,  spot_score     = self._analyse_spot()
        fut_trend, fut_cls, premium, fut_score = self._analyse_futures()
        opt_trend, opt_flow, opt_score        = self._analyse_options()
        sm_trend,  sm_score                   = self._analyse_smart_money()
        inst_trend, inst_score                = self._analyse_institution()
        global_score                          = self._analyse_global()
        news_sent, news_score                 = self._analyse_news()
        breadth_score                         = self._analyse_breadth()

        # --- Volume score ---
        f = self.futures
        if f.volume is not None and f.avg_volume is not None and f.avg_volume > 0:
            vol_score = min(10.0, round((f.volume / f.avg_volume) * 5, 2))
        else:
            vol_score = 5.0

        # --- AI Confirmation ---
        signal, reason = self._confirm_signal(
            spot_trend, fut_trend, opt_trend, opt_flow,
            inst_trend, sm_trend, news_sent, vol_score,
        )

        # --- Scores ---
        score = AIScore(
            spot         = spot_score,
            future       = fut_score,
            option_chain = opt_score,
            smart_money  = sm_score,
            institution  = inst_score,
            news         = news_score,
            volume       = vol_score,
            momentum     = (spot_score + vol_score) / 2,
            trend        = (spot_score + fut_score) / 2,
        )

        # --- Direction percentages ---
        bullish_votes = sum([
            spot_trend  == Trend.BULLISH,
            fut_trend   == Trend.BULLISH,
            opt_trend   == Trend.BULLISH,
            sm_trend    == Trend.BULLISH,
            inst_trend  == Trend.BULLISH,
            news_sent   == NewsSentiment.BULLISH,
        ])
        bearish_votes = sum([
            spot_trend  == Trend.BEARISH,
            fut_trend   == Trend.BEARISH,
            opt_trend   == Trend.BEARISH,
            sm_trend    == Trend.BEARISH,
            inst_trend  == Trend.BEARISH,
            news_sent   == NewsSentiment.BEARISH,
        ])
        total_votes   = 6
        neutral_votes = total_votes - bullish_votes - bearish_votes
        bullish_pct   = round(bullish_votes / total_votes * 100, 1)
        bearish_pct   = round(bearish_votes / total_votes * 100, 1)
        neutral_pct   = round(neutral_votes / total_votes * 100, 1)

        # Market direction
        if bullish_pct >= 60:
            market_direction = Trend.BULLISH
        elif bearish_pct >= 60:
            market_direction = Trend.BEARISH
        else:
            market_direction = Trend.NEUTRAL

        # --- Risk Management ---
        rm = self._compute_risk(signal)

        # --- Final Result ---
        result = AnalysisResult(
            bullish_pct  = bullish_pct,
            bearish_pct  = bearish_pct,
            neutral_pct  = neutral_pct,

            spot_trend         = spot_trend,
            future_trend       = fut_trend,
            option_chain_trend = opt_trend,
            institutional_flow = inst_trend,
            smart_money_dir    = sm_trend,

            entry     = rm.entry,
            stop_loss = rm.stop_loss,
            target_1  = rm.target_1,
            target_2  = rm.target_2,
            target_3  = rm.target_3,

            score      = score,
            confidence = score.confidence_pct,

            recommendation          = signal,
            reason                  = reason,
            futures_class           = fut_cls,
            option_flow             = opt_flow,
            futures_premium_discount = premium,
        )

        return result


# ==============================================================================
# PRETTY PRINT UTILITY
# ==============================================================================

def print_analysis(result: AnalysisResult) -> None:
    """Print a formatted institutional analysis report."""
    sep = "=" * 60
    print(sep)
    print("   INSTITUTIONAL AI ANALYSIS — NSE MARKET")
    print(sep)

    print(f"\n  MARKET DIRECTION")
    print(f"  ├─ Bullish  : {result.bullish_pct}%")
    print(f"  ├─ Bearish  : {result.bearish_pct}%")
    print(f"  └─ Neutral  : {result.neutral_pct}%")

    print(f"\n  TREND SIGNALS")
    print(f"  ├─ Spot            : {result.spot_trend.value}")
    print(f"  ├─ Future          : {result.future_trend.value}")
    print(f"  ├─ Option Chain    : {result.option_chain_trend.value}")
    print(f"  ├─ Institutional   : {result.institutional_flow.value}")
    print(f"  └─ Smart Money     : {result.smart_money_dir.value}")

    print(f"\n  FUTURES")
    print(f"  ├─ Classification  : {result.futures_class.value}")
    print(f"  └─ Premium/Disc    : {result.futures_premium_discount}")

    print(f"\n  OPTION FLOW        : {result.option_flow.value}")

    print(f"\n  AI SCORES  (0–10 per dimension)")
    s = result.score
    print(f"  ├─ Spot            : {s.spot}")
    print(f"  ├─ Future          : {s.future}")
    print(f"  ├─ Option Chain    : {s.option_chain}")
    print(f"  ├─ Smart Money     : {s.smart_money}")
    print(f"  ├─ Institution     : {s.institution}")
    print(f"  ├─ News            : {s.news}")
    print(f"  ├─ Volume          : {s.volume}")
    print(f"  ├─ Momentum        : {s.momentum}")
    print(f"  ├─ Trend           : {s.trend}")
    print(f"  ├─ Overall         : {s.overall}")
    print(f"  └─ Confidence      : {s.confidence_pct}%")

    print(f"\n  RISK MANAGEMENT")
    print(f"  ├─ Entry           : {result.entry}")
    print(f"  ├─ Stop Loss       : {result.stop_loss}")
    print(f"  ├─ Target 1        : {result.target_1}")
    print(f"  ├─ Target 2        : {result.target_2}")
    print(f"  └─ Target 3        : {result.target_3}")

    print(f"\n  ╔{'═'*38}╗")
    print(f"  ║  RECOMMENDATION  :  {result.recommendation.value:<16}  ║")
    print(f"  ║  CONFIDENCE      :  {result.confidence}%{' '*(14 - len(str(result.confidence)))}  ║")
    print(f"  ╚{'═'*38}╝")
    print(f"\n  REASON : {result.reason}")
    print(sep + "\n")


# ==============================================================================
# PUBLIC API — analyze_market()
# ==============================================================================

def analyze_market(
    spot:     dict | None = None,
    futures:  dict | None = None,
    option:   dict | None = None,
    smart:    dict | None = None,
    inst:     dict | None = None,
    global_:  dict | None = None,
    news:     dict | None = None,
    breadth:  dict | None = None,
) -> dict:
    """
    Single importable entry point used by option_chain.py and
    ai_market_intelligence.py.

    All arguments are plain dicts whose keys match the field names of
    the corresponding dataclass (e.g. SpotData, FuturesData …).
    Pass only the keys you have — every field is Optional.

    Returns a flat dict with the full analysis result so callers don't
    need to import the dataclasses themselves.

    Usage
    -----
    from ai_analysis_engine import analyze_market

    result = analyze_market(
        spot    = {"price": 24350, "rsi": 62.5, "atr": 120, ...},
        futures = {"futures_price": 24375, "spot_price": 24350, ...},
        option  = {"pcr": 1.15, "put_oi_change": 150000, ...},
        inst    = {"fii_buy": 4500, "fii_sell": 2100, ...},
        news    = {"sentiment": "BULLISH"},
        ...
    )

    print(result["recommendation"])   # "BUY" | "SELL" | "WAIT"
    print(result["confidence"])       # float  e.g. 78.4
    print(result["reason"])           # str
    """

    def _build(cls, data: dict | None):
        """Construct a dataclass from a dict, ignoring unknown keys."""
        if not data:
            return cls()
        import dataclasses
        valid = {f.name for f in dataclasses.fields(cls)}
        filtered = {k: v for k, v in data.items() if k in valid}
        # Convert "BULLISH"/"BEARISH"/"NEUTRAL" string for news sentiment
        if cls is NewsData and "sentiment" in filtered:
            raw = filtered["sentiment"]
            if isinstance(raw, str):
                # NewsSentiment values are "Bullish"/"Bearish"/"Neutral" (title-case)
                mapping = {
                    "BULLISH": NewsSentiment.BULLISH,
                    "BEARISH": NewsSentiment.BEARISH,
                    "NEUTRAL": NewsSentiment.NEUTRAL,
                    "Bullish": NewsSentiment.BULLISH,
                    "Bearish": NewsSentiment.BEARISH,
                    "Neutral": NewsSentiment.NEUTRAL,
                }
                filtered["sentiment"] = mapping.get(raw, NewsSentiment.NEUTRAL)
        return cls(**filtered)

    engine = AIAnalysisEngine(
        spot    = _build(SpotData,          spot),
        futures = _build(FuturesData,       futures),
        option  = _build(OptionChainData,   option),
        smart   = _build(SmartMoneyData,    smart),
        inst    = _build(InstitutionalData, inst),
        global_ = _build(GlobalData,        global_),
        news    = _build(NewsData,          news),
        breadth = _build(MarketBreadthData, breadth),
    )

    r = engine.run()

    return {
        # Direction
        "market_direction":   r.spot_trend.value,
        "bullish_pct":        r.bullish_pct,
        "bearish_pct":        r.bearish_pct,
        "neutral_pct":        r.neutral_pct,

        # Trend signals
        "spot_trend":         r.spot_trend.value,
        "future_trend":       r.future_trend.value,
        "option_chain_trend": r.option_chain_trend.value,
        "institutional_flow": r.institutional_flow.value,
        "smart_money_dir":    r.smart_money_dir.value,

        # Futures
        "futures_class":             r.futures_class.value,
        "futures_premium_discount":  r.futures_premium_discount,
        "option_flow":               r.option_flow.value,

        # Risk
        "entry":     r.entry,
        "stop_loss": r.stop_loss,
        "target_1":  r.target_1,
        "target_2":  r.target_2,
        "target_3":  r.target_3,

        # Scores
        "score_spot":         r.score.spot,
        "score_future":       r.score.future,
        "score_option_chain": r.score.option_chain,
        "score_smart_money":  r.score.smart_money,
        "score_institution":  r.score.institution,
        "score_news":         r.score.news,
        "score_volume":       r.score.volume,
        "score_momentum":     r.score.momentum,
        "score_trend":        r.score.trend,
        "score_overall":      r.score.overall,

        # Final signal
        "confidence":       r.confidence,
        "recommendation":   r.recommendation.value,
        "reason":           r.reason,
    }


# ==============================================================================
# QUICK-START EXAMPLE
# ==============================================================================

if __name__ == "__main__":
    # ── Replace these values with live FYERS data ──────────────────────
    engine = AIAnalysisEngine(
        spot = SpotData(
            price=24_350.0, open=24_200.0, high=24_420.0, low=24_150.0,
            close=24_340.0, vwap=24_280.0,
            ema20=24_100.0, ema50=23_900.0, ema200=22_500.0,
            rsi=62.5, macd=45.0, macd_signal=38.0,
            adx=28.0, atr=120.0,
            supertrend=24_100.0, supertrend_direction="UP",
        ),
        futures = FuturesData(
            futures_price=24_375.0, spot_price=24_350.0,
            open_interest=1_200_000, oi_change=50_000,
            volume=850_000, avg_volume=700_000,
        ),
        option = OptionChainData(
            pcr=1.15, max_pain=24_300.0,
            total_call_oi=2_500_000, total_put_oi=2_875_000,
            call_oi_change=80_000, put_oi_change=150_000,
            ce_volume=320_000, pe_volume=410_000,
            iv=14.5, delta=0.52, gamma=0.003,
            theta=-8.5, vega=12.0,
            gamma_exposure=5_000_000, delta_exposure=25_000_000,
        ),
        smart = SmartMoneyData(
            bos=True, choch=False,
            order_block="BULLISH", breaker_block=None,
            mitigation_block=None, fair_value_gap="BULLISH",
            liquidity_sweep="LOW",
            equal_high=False, equal_low=True,
            premium_zone=False, discount_zone=True,
        ),
        inst = InstitutionalData(
            fii_buy=4_500.0, fii_sell=2_100.0,
            dii_buy=1_800.0, dii_sell=1_500.0,
        ),
        global_ = GlobalData(
            india_vix=13.2, gift_nifty=24_380.0,
            usdinr=83.45, crude_oil=78.5, gold=2_340.0,
            us_market="BULLISH", asian_market="BULLISH",
        ),
        news = NewsData(
            headline="RBI keeps rates steady; inflation within target",
            sentiment=NewsSentiment.BULLISH,
            impact="MEDIUM",
        ),
        breadth = MarketBreadthData(
            advance=1_450, decline=620,
            sector_strength="IT, BANKING, AUTO leading",
        ),
    )

    result = engine.run()
    print_analysis(result)
