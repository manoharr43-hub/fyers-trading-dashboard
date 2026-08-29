"""
═════════════════════════════════════════════════════════════════════════════════
NSE OPTIONS CHAIN ANALYZER — Standalone Module for F&O Analysis
═════════════════════════════════════════════════════════════════════════════════

This module provides comprehensive options chain analysis including:
- Live PCR (Put-Call Ratio) analysis
- Max Pain calculation
- Institutional activity detection
- IV (Implied Volatility) analysis
- Greeks analysis
- Options bias/sentiment detection

Author: NSE AI PRO V17
Date: 2026
License: MIT
═════════════════════════════════════════════════════════════════════════════════
"""

import requests
import pandas as pd
import numpy as np
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Any, Optional
import os
import json

# ════════════════════════════════════════════════════════════════════════════════
# LOGGING CONFIGURATION
# ════════════════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("option_chain")

# ════════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ════════════════════════════════════════════════════════════════════════════════

DEFAULT_STRIKE_COUNT = 10
OPTIONS_HTTP_TIMEOUT = 15
MAX_PAIN_SAMPLE_SIZE = 50

# Greeks thresholds
DELTA_THRESHOLD = 0.50
GAMMA_THRESHOLD = 0.05
THETA_THRESHOLD = 0.02
VEGA_THRESHOLD = 0.20

# PCR thresholds
PCR_BULLISH_THRESHOLD = 0.80
PCR_BEARISH_THRESHOLD = 1.05
PCR_EXTREMELY_BULLISH = 0.65
PCR_EXTREMELY_BEARISH = 1.30

# OI change thresholds (percentage)
OI_BUILDUP_THRESHOLD = 10.0  # 10% increase
OI_UNWINDING_THRESHOLD = -10.0  # 10% decrease

# ════════════════════════════════════════════════════════════════════════════════
# DATA CLASSES
# ════════════════════════════════════════════════════════════════════════════════

class GreeksData:
    """Greeks data container (Delta, Gamma, Theta, Vega, Rho)"""
    def __init__(self, delta=0.0, gamma=0.0, theta=0.0, vega=0.0, rho=0.0):
        self.delta = float(delta or 0.0)
        self.gamma = float(gamma or 0.0)
        self.theta = float(theta or 0.0)
        self.vega = float(vega or 0.0)
        self.rho = float(rho or 0.0)
    
    def to_dict(self):
        return {
            "delta": round(self.delta, 4),
            "gamma": round(self.gamma, 4),
            "theta": round(self.theta, 4),
            "vega": round(self.vega, 4),
            "rho": round(self.rho, 4),
        }


class OptionLeg:
    """Single option leg (Call or Put)"""
    def __init__(self, data: Dict[str, Any]):
        self.strike_price = float(data.get("strike_price", 0))
        self.option_type = str(data.get("option_type", "")).upper()  # "CE" or "PE"
        self.ltp = float(data.get("ltp", 0))
        self.bid = float(data.get("bid", 0))
        self.ask = float(data.get("ask", 0))
        self.volume = float(data.get("volume", 0))
        self.oi = float(data.get("oi", 0))
        self.oich = float(data.get("oich", 0))  # OI change
        self.iv = float(data.get("iv", 0))  # Implied Volatility
        self.ltpch = float(data.get("ltpch", 0))  # Price change
        
        # Greeks (if available)
        greeks_data = data.get("greeks", {})
        self.greeks = GreeksData(
            delta=greeks_data.get("delta"),
            gamma=greeks_data.get("gamma"),
            theta=greeks_data.get("theta"),
            vega=greeks_data.get("vega"),
            rho=greeks_data.get("rho")
        )
    
    def is_call(self) -> bool:
        return self.option_type == "CE"
    
    def is_put(self) -> bool:
        return self.option_type == "PE"
    
    def mid_price(self) -> float:
        """Calculate mid price (average of bid and ask)"""
        if self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2.0
        return self.ltp
    
    def spread(self) -> float:
        """Calculate bid-ask spread"""
        if self.bid > 0 and self.ask > 0:
            return self.ask - self.bid
        return 0.0
    
    def to_dict(self):
        return {
            "strike": self.strike_price,
            "type": self.option_type,
            "ltp": round(self.ltp, 2),
            "bid": round(self.bid, 2),
            "ask": round(self.ask, 2),
            "volume": int(self.volume),
            "oi": int(self.oi),
            "oi_change": int(self.oich),
            "iv": round(self.iv, 2),
            "price_change": round(self.ltpch, 2),
            "greeks": self.greeks.to_dict(),
        }


# ════════════════════════════════════════════════════════════════════════════════
# FYERS OPTION CHAIN API
# ════════════════════════════════════════════════════════════════════════════════

def fetch_fyers_optionchain(
    symbol: str,
    access_token: str,
    app_id: str = "DEMO",
    strikecount: int = DEFAULT_STRIKE_COUNT,
    timestamp: str = "",
    greeks: bool = True
) -> Dict[str, Any]:
    """
    Fetch options chain directly from FYERS API v3
    
    Args:
        symbol: NSE symbol (e.g., "NSE:NIFTY50-INDEX")
        access_token: Fyers API access token
        app_id: Fyers app ID
        strikecount: Number of strikes on each side (5-50)
        timestamp: Optional expiry timestamp
        greeks: Whether to fetch Greeks data
    
    Returns:
        Dictionary with options chain data
    """
    try:
        headers = {
            "Authorization": f"{app_id}:{access_token}",
            "User-Agent": "NSE-AI-PRO-V17",
        }
        
        params = {
            "symbol": symbol,
            "strikecount": min(int(strikecount), 50),
            "greeks": "1" if greeks else "0",
        }
        
        if timestamp:
            params["timestamp"] = str(timestamp)
        
        url = "https://api-t1.fyers.in/data/options-chain-v3"
        
        response = requests.get(
            url,
            headers=headers,
            params=params,
            timeout=OPTIONS_HTTP_TIMEOUT
        )
        response.raise_for_status()
        
        data = response.json()
        logger.info(f"Fetched options chain for {symbol}")
        return data
    
    except requests.exceptions.Timeout:
        logger.error(f"Timeout fetching options chain for {symbol}")
        return {"s": "error", "message": "Request timeout"}
    
    except requests.exceptions.ConnectionError:
        logger.error(f"Connection error fetching options chain for {symbol}")
        return {"s": "error", "message": "Connection error"}
    
    except Exception as e:
        logger.error(f"Error fetching options chain for {symbol}: {e}")
        return {"s": "error", "message": str(e)}


# ════════════════════════════════════════════════════════════════════════════════
# OPTIONS CHAIN ANALYSIS FUNCTIONS
# ════════════════════════════════════════════════════════════════════════════════

def calculate_pcr(calls: List[OptionLeg], puts: List[OptionLeg], method: str = "oi") -> Optional[float]:
    """
    Calculate Put-Call Ratio (PCR)
    
    Args:
        calls: List of call option legs
        puts: List of put option legs
        method: "oi" for Open Interest, "volume" for Volume
    
    Returns:
        PCR value or None if insufficient data
    """
    try:
        if method == "oi":
            call_oi = sum(call.oi for call in calls)
            put_oi = sum(put.oi for put in puts)
            
            if call_oi == 0:
                return None
            
            return round(put_oi / call_oi, 3)
        
        elif method == "volume":
            call_vol = sum(call.volume for call in calls)
            put_vol = sum(put.volume for put in puts)
            
            if call_vol == 0:
                return None
            
            return round(put_vol / call_vol, 3)
    
    except Exception as e:
        logger.error(f"Error calculating PCR: {e}")
        return None


def analyze_pcr_bias(pcr: Optional[float]) -> Dict[str, Any]:
    """
    Analyze PCR and determine market bias
    
    Args:
        pcr: Put-Call Ratio value
    
    Returns:
        Dictionary with bias analysis
    """
    if pcr is None:
        return {
            "pcr": None,
            "bias": "NEUTRAL",
            "strength": 0,
            "interpretation": "Insufficient data",
        }
    
    try:
        if pcr <= PCR_EXTREMELY_BULLISH:
            bias = "🟢 EXTREMELY BULLISH"
            strength = 100
            interpretation = "Extreme put selling (bullish)"
        
        elif pcr <= PCR_BULLISH_THRESHOLD:
            bias = "🟢 BULLISH"
            strength = 75
            interpretation = "More puts than calls, but buying calls"
        
        elif pcr >= PCR_EXTREMELY_BEARISH:
            bias = "🔴 EXTREMELY BEARISH"
            strength = 100
            interpretation = "Extreme put buying (bearish)"
        
        elif pcr >= PCR_BEARISH_THRESHOLD:
            bias = "🔴 BEARISH"
            strength = 75
            interpretation = "More puts than calls, buying protection"
        
        else:
            bias = "🟡 NEUTRAL"
            strength = 50
            interpretation = "Balanced put-call activity"
        
        return {
            "pcr": pcr,
            "bias": bias,
            "strength": strength,
            "interpretation": interpretation,
        }
    
    except Exception as e:
        logger.error(f"Error analyzing PCR bias: {e}")
        return {
            "pcr": pcr,
            "bias": "ERROR",
            "strength": 0,
            "interpretation": str(e),
        }


def calculate_max_pain(calls: List[OptionLeg], puts: List[OptionLeg]) -> Optional[float]:
    """
    Calculate Max Pain level (price where both calls and puts lose maximum value)
    
    Args:
        calls: List of call option legs
        puts: List of put option legs
    
    Returns:
        Max pain strike price or None
    """
    try:
        if not calls or not puts:
            return None
        
        # Get all unique strikes
        strikes = sorted(set([opt.strike_price for opt in calls + puts]))
        
        if len(strikes) == 0:
            return None
        
        # Calculate pain for each strike
        pain_dict = {}
        
        for settlement_price in strikes:
            pain = 0.0
            
            # Call pain: (strike - settlement) * OI, only if settlement < strike
            for call in calls:
                if call.strike_price > settlement_price:
                    pain += (call.strike_price - settlement_price) * call.oi
            
            # Put pain: (settlement - strike) * OI, only if settlement < strike
            for put in puts:
                if settlement_price < put.strike_price:
                    pain += (put.strike_price - settlement_price) * put.oi
            
            pain_dict[settlement_price] = pain
        
        # Find strike with maximum pain
        max_pain_strike = min(pain_dict, key=pain_dict.get)
        logger.info(f"Max pain calculated: {max_pain_strike}")
        
        return float(max_pain_strike)
    
    except Exception as e:
        logger.error(f"Error calculating max pain: {e}")
        return None


def detect_oi_buildup(calls: List[OptionLeg], puts: List[OptionLeg]) -> Dict[str, Any]:
    """
    Detect Open Interest buildup (new positions being added)
    
    Args:
        calls: List of call option legs
        puts: List of put option legs
    
    Returns:
        Dictionary with buildup analysis
    """
    try:
        call_buildup = 0
        put_buildup = 0
        
        for call in calls:
            if call.oich > 0:  # OI increasing
                call_buildup += 1
        
        for put in puts:
            if put.oich > 0:  # OI increasing
                put_buildup += 1
        
        total_legs = len(calls) + len(puts)
        
        if total_legs == 0:
            return {
                "call_buildup": 0,
                "put_buildup": 0,
                "signal": "NONE",
                "interpretation": "No data",
            }
        
        buildup_pct = {
            "call": (call_buildup / len(calls) * 100) if calls else 0,
            "put": (put_buildup / len(puts) * 100) if puts else 0,
        }
        
        if call_buildup > put_buildup:
            signal = "🟢 CALL BUILDUP (BULLISH)"
            interpretation = f"New call positions being added ({buildup_pct['call']:.0f}% of calls)"
        
        elif put_buildup > call_buildup:
            signal = "🔴 PUT BUILDUP (BEARISH)"
            interpretation = f"New put positions being added ({buildup_pct['put']:.0f}% of puts)"
        
        else:
            signal = "🟡 BALANCED BUILDUP"
            interpretation = "Equal call and put buildup"
        
        return {
            "call_buildup_count": call_buildup,
            "put_buildup_count": put_buildup,
            "call_buildup_pct": round(buildup_pct["call"], 1),
            "put_buildup_pct": round(buildup_pct["put"], 1),
            "signal": signal,
            "interpretation": interpretation,
        }
    
    except Exception as e:
        logger.error(f"Error detecting OI buildup: {e}")
        return {
            "call_buildup_count": 0,
            "put_buildup_count": 0,
            "call_buildup_pct": 0,
            "put_buildup_pct": 0,
            "signal": "ERROR",
            "interpretation": str(e),
        }


def detect_oi_unwinding(calls: List[OptionLeg], puts: List[OptionLeg]) -> Dict[str, Any]:
    """
    Detect Open Interest unwinding (positions being closed)
    
    Args:
        calls: List of call option legs
        puts: List of put option legs
    
    Returns:
        Dictionary with unwinding analysis
    """
    try:
        call_unwinding = 0
        put_unwinding = 0
        
        for call in calls:
            if call.oich < 0:  # OI decreasing
                call_unwinding += 1
        
        for put in puts:
            if put.oich < 0:  # OI decreasing
                put_unwinding += 1
        
        total_legs = len(calls) + len(puts)
        
        if total_legs == 0:
            return {
                "call_unwinding": 0,
                "put_unwinding": 0,
                "signal": "NONE",
                "interpretation": "No data",
            }
        
        unwinding_pct = {
            "call": (call_unwinding / len(calls) * 100) if calls else 0,
            "put": (put_unwinding / len(puts) * 100) if puts else 0,
        }
        
        if call_unwinding > put_unwinding:
            signal = "🔴 CALL UNWINDING (BEARISH)"
            interpretation = f"Call positions being closed ({unwinding_pct['call']:.0f}% of calls)"
        
        elif put_unwinding > call_unwinding:
            signal = "🟢 PUT UNWINDING (BULLISH)"
            interpretation = f"Put positions being closed ({unwinding_pct['put']:.0f}% of puts)"
        
        else:
            signal = "🟡 BALANCED UNWINDING"
            interpretation = "Equal call and put unwinding"
        
        return {
            "call_unwinding_count": call_unwinding,
            "put_unwinding_count": put_unwinding,
            "call_unwinding_pct": round(unwinding_pct["call"], 1),
            "put_unwinding_pct": round(unwinding_pct["put"], 1),
            "signal": signal,
            "interpretation": interpretation,
        }
    
    except Exception as e:
        logger.error(f"Error detecting OI unwinding: {e}")
        return {
            "call_unwinding_count": 0,
            "put_unwinding_count": 0,
            "call_unwinding_pct": 0,
            "put_unwinding_pct": 0,
            "signal": "ERROR",
            "interpretation": str(e),
        }


def analyze_atm_greeks(calls: List[OptionLeg], puts: List[OptionLeg], spot: Optional[float]) -> Dict[str, Any]:
    """
    Analyze Greeks at ATM (At-The-Money) strike
    
    Args:
        calls: List of call option legs
        puts: List of put option legs
        spot: Current spot price
    
    Returns:
        Dictionary with ATM Greeks analysis
    """
    try:
        if not spot or spot <= 0:
            return {
                "atm_strike": None,
                "atm_call_delta": None,
                "atm_put_delta": None,
                "call_gamma": None,
                "put_gamma": None,
                "total_theta": None,
                "interpretation": "No spot price",
            }
        
        # Find ATM strike (closest to spot)
        all_strikes = sorted(set([opt.strike_price for opt in calls + puts]))
        atm_strike = min(all_strikes, key=lambda x: abs(x - spot))
        
        # Find ATM call and put
        atm_call = next((c for c in calls if c.strike_price == atm_strike), None)
        atm_put = next((p for p in puts if p.strike_price == atm_strike), None)
        
        if not atm_call or not atm_put:
            return {
                "atm_strike": atm_strike,
                "atm_call_delta": None,
                "atm_put_delta": None,
                "call_gamma": None,
                "put_gamma": None,
                "total_theta": None,
                "interpretation": "ATM option not found",
            }
        
        # Analyze Greeks
        call_delta = atm_call.greeks.delta
        put_delta = atm_put.greeks.delta
        call_gamma = atm_call.greeks.gamma
        put_gamma = atm_put.greeks.gamma
        call_theta = atm_call.greeks.theta
        put_theta = atm_put.greeks.theta
        
        total_theta = call_theta + put_theta
        
        # Interpretation
        if abs(call_delta) > DELTA_THRESHOLD:
            delta_interpretation = "Call is in-the-money"
        elif abs(call_delta) < 0.30:
            delta_interpretation = "Call is out-of-the-money"
        else:
            delta_interpretation = "Call is at-the-money"
        
        return {
            "atm_strike": atm_strike,
            "atm_call_delta": round(call_delta, 4),
            "atm_put_delta": round(put_delta, 4),
            "call_gamma": round(call_gamma, 4),
            "put_gamma": round(put_gamma, 4),
            "call_theta": round(call_theta, 4),
            "put_theta": round(put_theta, 4),
            "total_theta": round(total_theta, 4),
            "interpretation": delta_interpretation,
        }
    
    except Exception as e:
        logger.error(f"Error analyzing ATM Greeks: {e}")
        return {
            "atm_strike": None,
            "atm_call_delta": None,
            "atm_put_delta": None,
            "call_gamma": None,
            "put_gamma": None,
            "total_theta": None,
            "interpretation": f"Error: {str(e)[:100]}",
        }


def detect_iv_skew(calls: List[OptionLeg], puts: List[OptionLeg]) -> Dict[str, Any]:
    """
    Detect Implied Volatility skew (fear/greed indicators)
    
    Args:
        calls: List of call option legs
        puts: List of put option legs
    
    Returns:
        Dictionary with IV skew analysis
    """
    try:
        if not calls or not puts:
            return {
                "avg_call_iv": 0,
                "avg_put_iv": 0,
                "iv_skew": 0,
                "signal": "NONE",
                "interpretation": "Insufficient data",
            }
        
        avg_call_iv = np.mean([c.iv for c in calls if c.iv > 0]) if calls else 0
        avg_put_iv = np.mean([p.iv for p in puts if p.iv > 0]) if puts else 0
        
        iv_skew = avg_put_iv - avg_call_iv
        
        if iv_skew > 5:  # Puts have significantly higher IV
            signal = "🔴 FEAR SKEW"
            interpretation = "Puts trading at premium (fear of downside)"
        
        elif iv_skew < -5:  # Calls have significantly higher IV
            signal = "🟢 GREED SKEW"
            interpretation = "Calls trading at premium (greed for upside)"
        
        else:
            signal = "🟡 BALANCED IV"
            interpretation = "IV balanced between calls and puts"
        
        return {
            "avg_call_iv": round(avg_call_iv, 2),
            "avg_put_iv": round(avg_put_iv, 2),
            "iv_skew": round(iv_skew, 2),
            "signal": signal,
            "interpretation": interpretation,
        }
    
    except Exception as e:
        logger.error(f"Error detecting IV skew: {e}")
        return {
            "avg_call_iv": 0,
            "avg_put_iv": 0,
            "iv_skew": 0,
            "signal": "ERROR",
            "interpretation": str(e),
        }


# ════════════════════════════════════════════════════════════════════════════════
# COMPREHENSIVE OPTIONS CHAIN ANALYZER
# ════════════════════════════════════════════════════════════════════════════════

class OptionsChainAnalyzer:
    """Complete options chain analysis engine"""
    
    def __init__(self, access_token: str, app_id: str = "DEMO"):
        """
        Initialize analyzer
        
        Args:
            access_token: Fyers API access token
            app_id: Fyers app ID
        """
        self.access_token = access_token
        self.app_id = app_id
        self.logger = logger
    
    def analyze(
        self,
        symbol: str,
        strikecount: int = DEFAULT_STRIKE_COUNT,
        timestamp: str = ""
    ) -> Dict[str, Any]:
        """
        Complete options chain analysis
        
        Args:
            symbol: NSE symbol (e.g., "NSE:NIFTY50-INDEX")
            strikecount: Number of strikes to analyze
            timestamp: Optional expiry timestamp
        
        Returns:
            Comprehensive analysis dictionary
        """
        try:
            # Fetch data
            raw_data = fetch_fyers_optionchain(
                symbol=symbol,
                access_token=self.access_token,
                app_id=self.app_id,
                strikecount=strikecount,
                timestamp=timestamp,
                greeks=True
            )
            
            if raw_data.get("s") != "ok":
                return {
                    "status": "ERROR",
                    "symbol": symbol,
                    "message": raw_data.get("message", "Unknown error"),
                }
            
            # Parse chain data
            chain_data = raw_data.get("data", {})
            chain = chain_data.get("optionsChain", [])
            
            if not chain:
                return {
                    "status": "NO_DATA",
                    "symbol": symbol,
                    "message": "Empty options chain",
                }
            
            # Get spot price
            spot_row = next((x for x in chain if x.get("option_type", "") == ""), None)
            spot = float(spot_row.get("ltp")) if spot_row and spot_row.get("ltp") else None
            
            # Parse calls and puts
            calls = [OptionLeg(x) for x in chain if x.get("option_type") == "CE"]
            puts = [OptionLeg(x) for x in chain if x.get("option_type") == "PE"]
            
            # Run all analyses
            pcr = calculate_pcr(calls, puts, method="oi")
            pcr_analysis = analyze_pcr_bias(pcr)
            max_pain = calculate_max_pain(calls, puts)
            buildup = detect_oi_buildup(calls, puts)
            unwinding = detect_oi_unwinding(calls, puts)
            atm_greeks = analyze_atm_greeks(calls, puts, spot)
            iv_skew = detect_iv_skew(calls, puts)
            
            # Determine overall bias
            overall_bias = self._determine_overall_bias(
                pcr_analysis, buildup, unwinding, iv_skew, atm_greeks
            )
            
            return {
                "status": "OK",
                "symbol": symbol,
                "spot": round(spot, 2) if spot else None,
                "timestamp": datetime.now().isoformat(),
                
                # PCR Analysis
                "pcr": {
                    **pcr_analysis,
                    "interpretation_detail": self._pcr_interpretation(pcr),
                },
                
                # Max Pain
                "max_pain": round(max_pain, 2) if max_pain else None,
                "max_pain_diff_pct": round((abs(spot - max_pain) / spot * 100), 2) if spot and max_pain else None,
                
                # OI Analysis
                "oi_buildup": buildup,
                "oi_unwinding": unwinding,
                
                # Greeks Analysis
                "atm_greeks": atm_greeks,
                
                # IV Skew
                "iv_skew": iv_skew,
                
                # Overall Bias
                "overall_bias": overall_bias,
                
                # Data summary
                "call_count": len(calls),
                "put_count": len(puts),
                "total_call_oi": int(sum(c.oi for c in calls)),
                "total_put_oi": int(sum(p.oi for p in puts)),
                "total_call_volume": int(sum(c.volume for c in calls)),
                "total_put_volume": int(sum(p.volume for p in puts)),
                
                # Raw data (for detailed analysis)
                "calls": [c.to_dict() for c in calls],
                "puts": [p.to_dict() for p in puts],
            }
        
        except Exception as e:
            self.logger.error(f"Error in comprehensive analysis for {symbol}: {e}", exc_info=True)
            return {
                "status": "ERROR",
                "symbol": symbol,
                "message": str(e),
            }
    
    @staticmethod
    def _determine_overall_bias(pcr_data, buildup, unwinding, iv_skew, atm_greeks) -> Dict[str, Any]:
        """Determine overall market bias from all indicators"""
        try:
            score = 0.0
            factors = []
            
            # PCR factor (40%)
            pcr = pcr_data.get("pcr")
            if pcr:
                if pcr <= 0.80:
                    score += 40
                    factors.append("Bullish PCR")
                elif pcr >= 1.05:
                    score -= 40
                    factors.append("Bearish PCR")
            
            # OI Buildup factor (30%)
            buildup_sig = buildup.get("signal", "")
            if "CALL BUILDUP" in buildup_sig:
                score += 30
                factors.append("Call buildup")
            elif "PUT BUILDUP" in buildup_sig:
                score -= 30
                factors.append("Put buildup")
            
            # IV Skew factor (20%)
            iv_sig = iv_skew.get("signal", "")
            if "GREED" in iv_sig:
                score += 20
                factors.append("Greed skew")
            elif "FEAR" in iv_sig:
                score -= 20
                factors.append("Fear skew")
            
            # ATM Delta factor (10%)
            call_delta = atm_greeks.get("atm_call_delta")
            if call_delta and call_delta > 0.5:
                score += 10
                factors.append("ATM call ITM")
            elif call_delta and call_delta < 0.3:
                score -= 10
                factors.append("ATM call OTM")
            
            # Normalize score (-100 to +100)
            score = max(-100, min(100, score))
            
            # Determine bias
            if score >= 40:
                bias = "🟢 BULLISH"
            elif score <= -40:
                bias = "🔴 BEARISH"
            else:
                bias = "🟡 NEUTRAL"
            
            confidence = abs(score) / 100.0 * 100  # 0-100%
            
            return {
                "bias": bias,
                "score": round(score, 1),
                "confidence": round(confidence, 1),
                "factors": factors,
            }
        
        except Exception as e:
            logger.error(f"Error determining overall bias: {e}")
            return {
                "bias": "ERROR",
                "score": 0,
                "confidence": 0,
                "factors": [str(e)],
            }
    
    @staticmethod
    def _pcr_interpretation(pcr: Optional[float]) -> str:
        """Get detailed PCR interpretation"""
        if pcr is None:
            return "No PCR data"
        
        if pcr <= 0.65:
            return "Extremely bullish - puts being sold aggressively"
        elif pcr <= 0.80:
            return "Bullish - more puts being sold than bought"
        elif pcr <= 1.05:
            return "Neutral - balanced put-call activity"
        elif pcr <= 1.30:
            return "Bearish - more puts being bought than sold"
        else:
            return "Extremely bearish - puts being bought aggressively"


# ════════════════════════════════════════════════════════════════════════════════
# UTILITY FUNCTIONS
# ════════════════════════════════════════════════════════════════════════════════

def export_analysis_to_dict(analysis: Dict[str, Any]) -> Dict[str, Any]:
    """Convert analysis to JSON-serializable dictionary"""
    return json.loads(json.dumps(analysis, default=str))


def format_analysis_for_display(analysis: Dict[str, Any]) -> pd.DataFrame:
    """Format analysis results as DataFrame for display"""
    try:
        if analysis.get("status") != "OK":
            return pd.DataFrame({"Status": [analysis.get("message", "Unknown error")]})
        
        display_data = {
            "Metric": [
                "Symbol",
                "Spot Price",
                "PCR (OI)",
                "PCR Bias",
                "Max Pain",
                "Max Pain Diff %",
                "Call OI Buildup",
                "Put OI Buildup",
                "ATM Strike",
                "Call Delta",
                "Put Delta",
                "Avg Call IV",
                "Avg Put IV",
                "IV Skew",
                "Overall Bias",
                "Confidence %",
            ],
            "Value": [
                analysis.get("symbol", "N/A"),
                analysis.get("spot", "N/A"),
                analysis.get("pcr", {}).get("pcr", "N/A"),
                analysis.get("pcr", {}).get("bias", "N/A"),
                analysis.get("max_pain", "N/A"),
                analysis.get("max_pain_diff_pct", "N/A"),
                analysis.get("oi_buildup", {}).get("signal", "N/A"),
                analysis.get("oi_unwinding", {}).get("signal", "N/A"),
                analysis.get("atm_greeks", {}).get("atm_strike", "N/A"),
                analysis.get("atm_greeks", {}).get("atm_call_delta", "N/A"),
                analysis.get("atm_greeks", {}).get("atm_put_delta", "N/A"),
                analysis.get("iv_skew", {}).get("avg_call_iv", "N/A"),
                analysis.get("iv_skew", {}).get("avg_put_iv", "N/A"),
                analysis.get("iv_skew", {}).get("iv_skew", "N/A"),
                analysis.get("overall_bias", {}).get("bias", "N/A"),
                analysis.get("overall_bias", {}).get("confidence", "N/A"),
            ]
        }
        
        return pd.DataFrame(display_data)
    
    except Exception as e:
        logger.error(f"Error formatting analysis: {e}")
        return pd.DataFrame({"Error": [str(e)]})


# ════════════════════════════════════════════════════════════════════════════════
# EXAMPLE USAGE
# ════════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # Example usage
    print("═" * 80)
    print("NSE OPTIONS CHAIN ANALYZER - Example Usage")
    print("═" * 80)
    
    # Get credentials from environment
    access_token = os.environ.get("FYERS_ACCESS_TOKEN", "YOUR_TOKEN_HERE")
    app_id = os.environ.get("FYERS_APP_ID", "DEMO")
    
    if access_token == "YOUR_TOKEN_HERE":
        print("\n❌ Please set FYERS_ACCESS_TOKEN environment variable")
        print("   export FYERS_ACCESS_TOKEN='your_token_here'")
    else:
        # Initialize analyzer
        analyzer = OptionsChainAnalyzer(access_token, app_id)
        
        # Analyze NIFTY50 options
        print("\n📊 Analyzing NIFTY50 Options Chain...")
        analysis = analyzer.analyze("NSE:NIFTY50-INDEX", strikecount=10)
        
        # Display results
        print("\n" + "=" * 80)
        print(format_analysis_for_display(analysis).to_string(index=False))
        print("=" * 80)
        
        # Export to JSON
        json_data = export_analysis_to_dict(analysis)
        print("\n📋 Full Analysis (JSON):")
        print(json.dumps(json_data, indent=2))
