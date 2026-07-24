"""
================================================================================
 ai_market_intelligence.py
 "🧠 AI Market Intelligence" page — F&O TOP MOVERS BUY/SELL ENGINE

 Plugs into app.py's page router as:
     from ai_market_intelligence import show_ai_market_intelligence
     show_ai_market_intelligence(fyers)

 `fyers` is an already-authenticated `fyersModel.FyersModel` instance
 (created in app.py after login). All market data in this module is
 pulled through that instance (quotes / history / optionchain) so no
 separate API keys or scraping are needed for price/OI data.

 HONEST DATA DISCLAIMER
 -----------------------
 Fyers' public API does not expose certified institutional order-flow,
 official NSE delivery %, or FII/DII ledgers. Columns like "Institutional
 Score", "Smart Money Score" and "Delivery Score" are computed from
 public price/volume/OI data using standard proxies (see fno_engine.py
 docstring). "Expected ROI"/"Expected Accuracy" are model estimates, not
 guarantees. This is a research/decision-support tool, not investment
 advice — always manage your own risk.
================================================================================
"""

from __future__ import annotations

import io
import math
import datetime as dt
from typing import Optional

import numpy as np
import pandas as pd
import requests
import streamlit as st

import fno_engine as eng

try:
    from streamlit_autorefresh import st_autorefresh
    AUTOREFRESH_AVAILABLE = True
except Exception:
    AUTOREFRESH_AVAILABLE = False


# ============================================================================
# CONFIG
# ============================================================================

DEFAULT_CAPITAL = 100000.0
DEFAULT_RISK_PCT = 1.0
HIST_LOOKBACK_DAYS = 400

FNO_SYMBOLS = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "SBIN", "AXISBANK",
    "KOTAKBANK", "HINDUNILVR", "ITC", "BHARTIARTL", "LT", "BAJFINANCE",
    "BAJAJFINSV", "ASIANPAINT", "MARUTI", "TITAN", "SUNPHARMA", "ULTRACEMCO",
    "NESTLEIND", "WIPRO", "HCLTECH", "TECHM", "ADANIENT", "ADANIPORTS",
    "ADANIGREEN", "ADANIPOWER", "TATASTEEL", "TATAMOTORS", "TATACONSUM",
    "TATAPOWER", "JSWSTEEL", "HINDALCO", "COALINDIA", "NTPC", "POWERGRID",
    "ONGC", "BPCL", "IOC", "GRASIM", "CIPLA", "DRREDDY", "DIVISLAB",
    "APOLLOHOSP", "EICHERMOT", "HEROMOTOCO", "BAJAJ-AUTO", "M&M", "UPL",
    "SBILIFE", "HDFCLIFE", "ICICIPRULI", "ICICIGI", "SHREECEM", "AMBUJACEM",
    "ACC", "DLF", "GODREJPROP", "PIDILITIND", "HAVELLS", "VOLTAS", "DIXON",
    "POLYCAB", "SIEMENS", "ABB", "CUMMINSIND", "BEL", "HAL", "BHEL",
    "IRCTC", "INDIGO", "ZOMATO", "NYKAA", "PAYTM", "POLICYBZR", "TRENT",
    "DMART", "JUBLFOOD", "COLPAL", "MARICO", "DABUR", "GODREJCP",
    "BRITANNIA", "LTIM", "PERSISTENT", "COFORGE", "MPHASIS", "NAUKRI",
    "INDUSINDBK", "FEDERALBNK", "BANDHANBNK", "IDFCFIRSTB", "PNB",
    "BANKBARODA", "CANBK", "AUBANK", "CHOLAFIN", "MUTHOOTFIN", "PFC",
    "RECLTD", "SAIL", "NMDC", "VEDL", "JINDALSTEL", "GAIL", "PETRONET",
    "CONCOR", "CROMPTON", "ASHOKLEY", "TVSMOTOR", "BALKRISIND", "MRF",
    "BOSCHLTD", "MOTHERSON", "BHARATFORG", "LUPIN", "AUROPHARMA", "ALKEM",
    "BIOCON", "MFSL", "ABCAPITAL", "ABFRL", "PAGEIND", "ASTRAL", "SRF",
    "DEEPAKNTR", "COROMANDEL", "CDSL", "BSE", "MCX", "ANGELONE", "HDFCAMC",
]
FNO_SYMBOLS = sorted(set(FNO_SYMBOLS))


# ============================================================================
# FYERS DATA ADAPTERS (all defensive — never raise, always degrade safely)
# ============================================================================

def _fy_symbol(symbol: str) -> str:
    return f"NSE:{symbol}-EQ"


@st.cache_data(ttl=180, show_spinner=False)
def fetch_price_history_fyers(_fyers, symbol: str) -> Optional[pd.DataFrame]:
    """Daily OHLCV via Fyers `.history()`. Cache key excludes `_fyers`
    (leading underscore) since the client object isn't hashable."""
    try:
        to_date = dt.date.today()
        from_date = to_date - dt.timedelta(days=HIST_LOOKBACK_DAYS)
        data = {
            "symbol": _fy_symbol(symbol),
            "resolution": "D",
            "date_format": "1",
            "range_from": from_date.strftime("%Y-%m-%d"),
            "range_to": to_date.strftime("%Y-%m-%d"),
            "cont_flag": "1",
        }
        resp = _fyers.history(data=data)
        if not resp or resp.get("s") != "ok" or not resp.get("candles"):
            return None
        df = pd.DataFrame(resp["candles"], columns=["ts", "Open", "High", "Low", "Close", "Volume"])
        df["ts"] = pd.to_datetime(df["ts"], unit="s")
        df = df.set_index("ts").sort_index()
        df = df.dropna(how="all")
        if len(df) < 30:
            return None
        return df
    except Exception:
        return None


@st.cache_data(ttl=30, show_spinner=False)
def fetch_quote_fyers(_fyers, symbol: str) -> float:
    """Latest traded price via `.quotes()`; falls back to NaN on failure
    (caller falls back further to the last historical close)."""
    try:
        resp = _fyers.quotes(data={"symbols": _fy_symbol(symbol)})
        if not resp or resp.get("s") != "ok" or not resp.get("d"):
            return float("nan")
        return eng.safe_float(resp["d"][0].get("v", {}).get("lp"), float("nan"))
    except Exception:
        return float("nan")


@st.cache_data(ttl=180, show_spinner=False)
def fetch_option_chain_fyers(_fyers, symbol: str) -> Optional[dict]:
    """Raw Fyers option-chain response for the nearest expiry. Returns
    None on any failure (missing method on older SDK builds, symbol not
    F&O-enabled, network hiccup, etc.) — caller must degrade gracefully."""
    try:
        if not hasattr(_fyers, "optionchain"):
            return None
        resp = _fyers.optionchain(data={
            "symbol": _fy_symbol(symbol), "strikecount": 15, "timestamp": "",
        })
        if not resp or resp.get("s") != "ok" or "data" not in resp:
            return None
        return resp["data"]
    except Exception:
        return None


def parse_fyers_option_chain(chain_data: Optional[dict], spot: float) -> dict:
    """Converts the raw Fyers optionchain `data` block into the
    normalized row list `fno_engine.normalize_option_chain_generic` expects."""
    if not chain_data:
        return eng.normalize_option_chain_generic([], spot, "N/A")
    try:
        options = chain_data.get("optionsChain", []) or []
        expiry = "N/A"
        expiry_list = chain_data.get("expiryData", [])
        if expiry_list:
            expiry = str(expiry_list[0].get("date", expiry_list[0].get("expiry", "N/A")))

        by_strike: dict[float, dict] = {}
        for row in options:
            opt_type = row.get("option_type", "")
            if opt_type not in ("CE", "PE"):
                continue
            strike = eng.safe_float(row.get("strike_price"), -1)
            if strike <= 0:
                continue
            entry = by_strike.setdefault(strike, {
                "strike": strike, "ce_oi": 0.0, "pe_oi": 0.0,
                "ce_oi_chg": 0.0, "pe_oi_chg": 0.0,
                "ce_iv": float("nan"), "pe_iv": float("nan"),
                "ce_ltp": float("nan"), "pe_ltp": float("nan"),
            })
            if opt_type == "CE":
                entry["ce_oi"] = eng.safe_float(row.get("oi"))
                entry["ce_oi_chg"] = eng.safe_float(row.get("oich"))
                entry["ce_ltp"] = eng.safe_float(row.get("ltp"), float("nan"))
            else:
                entry["pe_oi"] = eng.safe_float(row.get("oi"))
                entry["pe_oi_chg"] = eng.safe_float(row.get("oich"))
                entry["pe_ltp"] = eng.safe_float(row.get("ltp"), float("nan"))

        rows = list(by_strike.values())
        return eng.normalize_option_chain_generic(rows, spot, expiry)
    except Exception:
        return eng.normalize_option_chain_generic([], spot, "N/A")


@st.cache_data(ttl=180, show_spinner=False)
def fetch_india_vix_fyers(_fyers) -> float:
    try:
        resp = _fyers.quotes(data={"symbols": "NSE:INDIAVIX-INDEX"})
        if not resp or resp.get("s") != "ok" or not resp.get("d"):
            return float("nan")
        return eng.safe_float(resp["d"][0].get("v", {}).get("lp"), float("nan"))
    except Exception:
        return float("nan")


@st.cache_data(ttl=180, show_spinner=False)
def fetch_index_change_pct_fyers(_fyers) -> float:
    try:
        resp = _fyers.quotes(data={"symbols": "NSE:NIFTY50-INDEX"})
        if not resp or resp.get("s") != "ok" or not resp.get("d"):
            return 0.0
        v = resp["d"][0].get("v", {})
        return eng.safe_float(v.get("chp"), 0.0)  # % change field
    except Exception:
        return 0.0


@st.cache_data(ttl=900, show_spinner=False)
def fetch_fii_dii_best_effort() -> dict:
    """Best-effort public FII/DII net-flow snapshot (Rs crore). Returns
    NaN if unavailable rather than fabricating numbers — this is not a
    Fyers endpoint, since broker APIs don't expose FII/DII ledgers."""
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get("https://www.nseindia.com/api/fiidiiTradeReact", headers=headers, timeout=5)
        if r.status_code != 200:
            return {"fii_net": float("nan"), "dii_net": float("nan")}
        data = r.json()
        if not data or len(data) < 2:
            return {"fii_net": float("nan"), "dii_net": float("nan")}
        fii_row = next((d for d in data if "FII" in d.get("category", "")), None)
        dii_row = next((d for d in data if "DII" in d.get("category", "")), None)
        fii_net = eng.safe_float(fii_row.get("netValue"), float("nan")) if fii_row else float("nan")
        dii_net = eng.safe_float(dii_row.get("netValue"), float("nan")) if dii_row else float("nan")
        return {"fii_net": fii_net, "dii_net": dii_net}
    except Exception:
        return {"fii_net": float("nan"), "dii_net": float("nan")}


@st.cache_data(ttl=900, show_spinner=False)
def fetch_news_sentiment(symbol: str) -> float:
    """Lightweight keyword-based headline sentiment proxy in [-1, 1].
    Independent of the broker; returns 0.0 (neutral) if headlines can't
    be retrieved rather than fabricating a score."""
    positive_kw = ["upgrade", "beats", "surge", "record", "growth", "buy",
                   "outperform", "rally", "profit rise", "strong", "wins",
                   "expansion", "bullish"]
    negative_kw = ["downgrade", "misses", "plunge", "loss", "sell", "probe",
                   "weak", "decline", "cut", "bearish", "fraud", "default"]
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        url = f"https://news.google.com/rss/search?q={symbol}+NSE+stock&hl=en-IN&gl=IN"
        r = requests.get(url, headers=headers, timeout=5)
        if r.status_code != 200:
            return 0.0
        text = r.text.lower()
        pos = sum(text.count(k) for k in positive_kw)
        neg = sum(text.count(k) for k in negative_kw)
        total = pos + neg
        if total == 0:
            return 0.0
        return round(eng.clip((pos - neg) / total, -1, 1), 3)
    except Exception:
        return 0.0


# ============================================================================
# PER-SYMBOL SCAN
# ============================================================================

def scan_symbol(fyers, symbol: str, market_ctx_score: float, capital: float, risk_pct: float) -> Optional[dict]:
    df = fetch_price_history_fyers(fyers, symbol)
    if df is None or len(df) < 30:
        return None

    try:
        feat = eng.build_feature_row(df)
        live_price = fetch_quote_fyers(fyers, symbol)
        spot = live_price if not eng.is_nan(live_price) and live_price > 0 else feat["close"]
        if spot <= 0:
            return None

        structure_info = eng.market_structure_analysis(df)
        structure = structure_info["structure"]
        trend = eng.trend_label(structure, feat)

        delivery_pct = eng.delivery_proxy_pct(df)
        chain_raw = fetch_option_chain_fyers(fyers, symbol)
        opt = parse_fyers_option_chain(chain_raw, spot)
        news_sent = fetch_news_sentiment(symbol)

        # Fyers option chain does not expose IV -> fall back to realized
        # volatility as an IV proxy for Greeks / option pricing / IV score.
        iv_for_scoring = opt["avg_iv"] if not eng.is_nan(opt["avg_iv"]) else feat["realized_vol_pct"]

        mom_s = eng.momentum_score(feat)
        vol_s = eng.volume_score(feat)
        del_s = eng.delivery_score(delivery_pct)
        direction_hint = 1 if feat["close"] > feat["ema50"] else -1
        oi_s = eng.oi_score(opt, direction_hint)
        pcr_s = eng.pcr_score(opt["pcr"])
        iv_s = eng.iv_score(iv_for_scoring, float("nan"))  # India VIX applied at market level below
        smc_s = eng.smart_money_score(del_s, feat["rel_vol"], oi_s, structure)

        ml_proba = eng.xgb_probability(df)

        rule_components = {
            "momentum": (mom_s, 0.16),
            "volume": (vol_s, 0.10),
            "delivery": (del_s, 0.08),
            "oi": (oi_s, 0.12),
            "pcr": (pcr_s, 0.08),
            "iv": (iv_s, 0.05),
            "smart_money": (smc_s, 0.14),
            "market_context": (market_ctx_score, 0.09),
            "adx_trend": (eng.clip(50 + (feat["adx"] - 20) * (1 if direction_hint > 0 else -1), 0, 100), 0.08),
            "supertrend": (70 if feat["supertrend_dir"] == 1 else 30, 0.06),
            "news": (eng.clip(50 + news_sent * 30, 0, 100), 0.04),
        }
        rule_bull_pct = sum(v * w for v, w in rule_components.values())

        if ml_proba is not None:
            final_bull_pct = 0.55 * rule_bull_pct + 0.45 * (ml_proba * 100)
            model_used = "SMC+TA Ensemble + XGBoost"
        else:
            final_bull_pct = rule_bull_pct
            model_used = "SMC+TA Ensemble (rule-based)"

        final_bull_pct = eng.clip(final_bull_pct, 1, 99)
        direction_label = eng.classify_direction(final_bull_pct)
        # The BUY/SELL split is anchored at 60, not 50, so it lines up
        # exactly with the Neutral/Bearish band boundary used above --
        # otherwise a 51-59% score would be labelled BUY (>=50) while its
        # Direction Class says "Bearish" (40-59% band), which is self-
        # contradictory. >=60 keeps AI Direction and Direction Class
        # always in agreement (Bullish/Strong Bullish/Neutral -> BUY,
        # Bearish/Strong Bearish -> SELL).
        is_bullish_side = final_bull_pct >= 60
        confidence = final_bull_pct if is_bullish_side else 100 - final_bull_pct

        institutional_score = eng.clip(0.35 * smc_s + 0.25 * oi_s + 0.20 * del_s + 0.20 * vol_s, 0, 100)

        breakout_prob = eng.clip(confidence * 0.9 + (feat["adx"] - 20) * 0.5, 0, 100) if is_bullish_side else eng.clip(30 - (confidence - 50), 0, 100)
        breakdown_prob = eng.clip(confidence * 0.9 + (feat["adx"] - 20) * 0.5, 0, 100) if not is_bullish_side else eng.clip(30 - (confidence - 50), 0, 100)
        swing_prob = eng.clip(0.5 * confidence + 0.5 * institutional_score, 0, 100)
        intraday_prob = eng.clip(0.6 * vol_s + 0.4 * mom_s, 0, 100)
        position_trade_prob = eng.clip(0.5 * institutional_score + 0.5 * (100 - abs(feat["adx"] - 25)), 0, 100)

        option_buying_score = eng.clip(confidence * 0.6 + (100 - iv_s) * 0.4, 0, 100)
        option_selling_score = eng.clip((100 - confidence) * 0.3 + iv_s * 0.7, 0, 100) if not eng.is_nan(opt["avg_iv"]) or not eng.is_nan(feat["realized_vol_pct"]) else 50.0

        plan = eng.build_trade_plan(spot, feat["atr"], direction_label, confidence, opt,
                                     iv_for_scoring, symbol, capital, risk_pct)

        max_pain_distance = (eng.safe_round(eng.pct(spot - opt["max_pain"], opt["max_pain"]))
                              if not eng.is_nan(opt["max_pain"]) and opt["max_pain"] else "N/A")

        buy_sell_strength = eng.safe_round(confidence if is_bullish_side else -confidence, 1)

        remarks_bits = [structure]
        for key in ("bos", "choch", "liquidity_sweep", "fvg"):
            if structure_info[key] != "None":
                remarks_bits.append(structure_info[key])
        remarks_bits.append(f"RSI {eng.safe_round(feat['rsi'],1)}")
        remarks_bits.append(f"RelVol {eng.safe_round(feat['rel_vol'],2)}x")
        if not opt["available"]:
            remarks_bits.append("Option chain unavailable on this Fyers plan/symbol - equity signals only")
        elif eng.is_nan(opt["avg_iv"]):
            remarks_bits.append("Broker IV not available - used realized-vol proxy")
        remarks_bits.append(model_used)
        ai_remarks = " | ".join(remarks_bits)

        return {
            "Symbol": symbol,
            "Spot Price": eng.safe_round(spot, 2),
            "AI Direction": "BUY" if is_bullish_side else "SELL",
            "AI Confidence %": eng.safe_round(confidence, 2),
            "Institutional Score": eng.safe_round(institutional_score, 2),
            "Trend": trend,
            "Direction Class": direction_label,
            "Entry Price": eng.safe_round(plan.entry, 2),
            "Stop Loss": eng.safe_round(plan.stop_loss, 2),
            "Target 1": eng.safe_round(plan.target1, 2),
            "Target 2": eng.safe_round(plan.target2, 2),
            "Target 3": eng.safe_round(plan.target3, 2),
            "Risk Reward Ratio": plan.rr,
            "Expected Move %": plan.expected_move_pct,
            "Expected Points": plan.expected_points,
            "Buy/Sell Strength": buy_sell_strength,
            "Momentum Score": eng.safe_round(mom_s, 1),
            "Volume Score": eng.safe_round(vol_s, 1),
            "Delivery Score": eng.safe_round(del_s, 1),
            "OI Score": eng.safe_round(oi_s, 1),
            "PCR Score": eng.safe_round(pcr_s, 1),
            "IV Score": eng.safe_round(iv_s, 1),
            "Smart Money Score": eng.safe_round(smc_s, 1),
            "Breakout Probability": eng.safe_round(breakout_prob, 1),
            "Breakdown Probability": eng.safe_round(breakdown_prob, 1),
            "Swing Probability": eng.safe_round(swing_prob, 1),
            "Intraday Probability": eng.safe_round(intraday_prob, 1),
            "Position Trade Probability": eng.safe_round(position_trade_prob, 1),
            "Option Buying Score": eng.safe_round(option_buying_score, 1),
            "Option Selling Score": eng.safe_round(option_selling_score, 1),
            "Option Writers Bias": opt["writers_bias"],
            "Max Pain Distance": max_pain_distance,
            "ATM Strike": opt["atm_strike"] if not eng.is_nan(opt["atm_strike"]) else "N/A",
            "ITM Strike": opt["itm_strike"] if not eng.is_nan(opt["itm_strike"]) else "N/A",
            "OTM Strike": opt["otm_strike"] if not eng.is_nan(opt["otm_strike"]) else "N/A",
            "Recommended CE Strike": plan.ce_strike,
            "Recommended PE Strike": plan.pe_strike,
            "CE Entry": plan.ce_entry,
            "CE SL": plan.ce_sl,
            "CE Target": plan.ce_target,
            "PE Entry": plan.pe_entry,
            "PE SL": plan.pe_sl,
            "PE Target": plan.pe_target,
            "Best Expiry": opt["best_expiry"],
            "Suggested Quantity": plan.qty,
            "Suggested Capital": plan.capital,
            "Expected ROI": plan.roi,
            "Expected Accuracy": eng.safe_round(min(confidence, 92.0), 1),
            "AI Remarks": ai_remarks,
            "_rel_vol_raw": feat["rel_vol"],
            "_oi_conf_raw": oi_s,
        }
    except Exception as exc:
        return {
            "Symbol": symbol, "Spot Price": "N/A", "AI Direction": "N/A",
            "AI Confidence %": 0.0, "Institutional Score": 0.0, "Trend": "N/A",
            "Direction Class": "N/A", "Entry Price": "N/A", "Stop Loss": "N/A",
            "Target 1": "N/A", "Target 2": "N/A", "Target 3": "N/A",
            "Risk Reward Ratio": 0.0, "Expected Move %": 0.0, "Expected Points": 0.0,
            "Buy/Sell Strength": 0.0, "Momentum Score": 0.0, "Volume Score": 0.0,
            "Delivery Score": 0.0, "OI Score": 0.0, "PCR Score": 0.0, "IV Score": 0.0,
            "Smart Money Score": 0.0, "Breakout Probability": 0.0, "Breakdown Probability": 0.0,
            "Swing Probability": 0.0, "Intraday Probability": 0.0, "Position Trade Probability": 0.0,
            "Option Buying Score": 0.0, "Option Selling Score": 0.0, "Option Writers Bias": "Unknown",
            "Max Pain Distance": "N/A", "ATM Strike": "N/A", "ITM Strike": "N/A", "OTM Strike": "N/A",
            "Recommended CE Strike": "N/A", "Recommended PE Strike": "N/A", "CE Entry": "N/A",
            "CE SL": "N/A", "CE Target": "N/A", "PE Entry": "N/A", "PE SL": "N/A", "PE Target": "N/A",
            "Best Expiry": "N/A", "Suggested Quantity": 0, "Suggested Capital": 0.0,
            "Expected ROI": 0.0, "Expected Accuracy": 0.0,
            "AI Remarks": f"Data error for this symbol ({type(exc).__name__}) - skipped scoring",
            "_rel_vol_raw": 1.0, "_oi_conf_raw": 50.0,
        }


def run_full_scan(fyers, symbols: list, capital: float, risk_pct: float, progress_cb=None) -> pd.DataFrame:
    india_vix = fetch_india_vix_fyers(fyers)
    index_change_pct = fetch_index_change_pct_fyers(fyers)
    fii_dii = fetch_fii_dii_best_effort()
    market_ctx_score = eng.market_context_score(india_vix, index_change_pct, fii_dii["fii_net"], fii_dii["dii_net"])

    rows, seen, total = [], set(), len(symbols)
    for i, sym in enumerate(symbols):
        if sym in seen:
            continue
        seen.add(sym)
        row = scan_symbol(fyers, sym, market_ctx_score, capital, risk_pct)
        if row is not None:
            rows.append(row)
        if progress_cb:
            progress_cb((i + 1) / total, sym)

    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).drop_duplicates(subset=["Symbol"], keep="first").fillna("N/A")


def rank_and_split(df: pd.DataFrame):
    if df.empty:
        return pd.DataFrame(), pd.DataFrame()
    work = df.copy()
    for col in ["AI Confidence %", "Institutional Score", "_rel_vol_raw", "_oi_conf_raw"]:
        work[col] = pd.to_numeric(work[col], errors="coerce").fillna(0)

    buys = work[work["AI Direction"] == "BUY"].sort_values(
        by=["AI Confidence %", "Institutional Score", "_rel_vol_raw", "_oi_conf_raw"],
        ascending=[False, False, False, False],
    ).head(10).reset_index(drop=True)

    sells = work[work["AI Direction"] == "SELL"].sort_values(
        by=["AI Confidence %", "Institutional Score", "_rel_vol_raw", "_oi_conf_raw"],
        ascending=[False, False, False, False],
    ).head(10).reset_index(drop=True)

    for tbl in (buys, sells):
        tbl.insert(0, "Rank", range(1, len(tbl) + 1))
        tbl.drop(columns=["_rel_vol_raw", "_oi_conf_raw"], inplace=True, errors="ignore")

    return buys, sells


# ============================================================================
# UI HELPERS
# ============================================================================

def to_excel_bytes(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    try:
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="Signals")
    except Exception:
        with pd.ExcelWriter(buf, engine="xlsxwriter") as writer:
            df.to_excel(writer, index=False, sheet_name="Signals")
    return buf.getvalue()


def style_table(df: pd.DataFrame, side: str):
    if df.empty:
        return df
    accent = "#0ecb81" if side == "BUY" else "#f6465d"

    def highlight_conf(val):
        try:
            v = float(val)
        except Exception:
            return ""
        if v >= 90:
            return "background-color:#0ecb81;color:white;font-weight:600"
        if v >= 80:
            return "background-color:#7cd992;color:black"
        if v >= 60:
            return "background-color:#fff3b0;color:black"
        if v >= 40:
            return "background-color:#ffc48a;color:black"
        return "background-color:#f6465d;color:white"

    def highlight_dir(val):
        if val == "BUY":
            return "color:#0ecb81;font-weight:700"
        if val == "SELL":
            return "color:#f6465d;font-weight:700"
        return ""

    return (
        df.style
        .applymap(highlight_conf, subset=["AI Confidence %"])
        .applymap(highlight_dir, subset=["AI Direction"])
        .set_properties(**{"font-size": "13px"})
        .set_table_styles([{"selector": "th", "props": [("background-color", accent), ("color", "white")]}])
        .format(precision=2)
    )


def render_table_block(title: str, df: pd.DataFrame, side: str, key_prefix: str):
    st.subheader(title)
    if df.empty:
        st.info("No qualifying signals found in the current scan for this side.")
        return

    search = st.text_input(f"🔍 Search {side} table (symbol / remarks)", key=f"{key_prefix}_search")
    view_df = df.copy()
    if search:
        mask = view_df.apply(lambda r: search.lower() in str(r).lower(), axis=1)
        view_df = view_df[mask]

    sort_col = st.selectbox(
        f"Sort {side} table by", options=list(view_df.columns),
        index=list(view_df.columns).index("AI Confidence %") if "AI Confidence %" in view_df.columns else 0,
        key=f"{key_prefix}_sort_col",
    )
    sort_asc = st.checkbox(f"Ascending order ({side})", value=False, key=f"{key_prefix}_sort_asc")
    try:
        view_df = view_df.sort_values(by=sort_col, ascending=sort_asc, key=lambda s: pd.to_numeric(s, errors="ignore"))
    except Exception:
        view_df = view_df.sort_values(by=sort_col, ascending=sort_asc)

    st.dataframe(style_table(view_df, side), use_container_width=True, height=420)

    col1, col2 = st.columns(2)
    with col1:
        st.download_button(
            f"⬇️ Export {side} CSV", data=view_df.to_csv(index=False).encode("utf-8"),
            file_name=f"fno_{side.lower()}_signals_{dt.date.today()}.csv",
            mime="text/csv", key=f"{key_prefix}_csv",
        )
    with col2:
        st.download_button(
            f"⬇️ Export {side} Excel", data=to_excel_bytes(view_df),
            file_name=f"fno_{side.lower()}_signals_{dt.date.today()}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=f"{key_prefix}_xlsx",
        )


# ============================================================================
# PAGE ENTRY POINT
# ============================================================================

def show_ai_market_intelligence(fyers) -> None:
    st.title("🧠 AI Market Intelligence — F&O TOP MOVERS BUY/SELL ENGINE")

    with st.expander("⚠️ Data & methodology disclaimer (please read)"):
        st.write(
            "Price, volume and option-OI data are pulled live from your Fyers account via "
            "the Fyers API. Fyers does not expose certified institutional order-flow, official "
            "NSE delivery %, or FII/DII ledgers, so 'Institutional Score', 'Smart Money Score' "
            "and 'Delivery Score' are computed from public price/volume/OI proxies, and implied "
            "volatility falls back to a realized-volatility proxy when the broker doesn't supply "
            "it for a symbol. This is a research/decision-support tool, not investment advice — "
            "'Expected ROI' and 'Expected Accuracy' are model estimates, not guarantees."
        )

    with st.sidebar:
        st.header("⚙️ Engine Settings")
        capital = st.number_input("Capital (₹)", min_value=10000.0, value=DEFAULT_CAPITAL, step=10000.0, key="fno_capital")
        risk_pct = st.slider("Risk per trade (%)", 0.25, 5.0, DEFAULT_RISK_PCT, 0.25, key="fno_risk")
        universe_choice = st.radio("Scan universe", ["Full F&O list", "Custom subset"], index=0, key="fno_universe_choice")
        if universe_choice == "Custom subset":
            symbols = st.multiselect("Choose symbols", FNO_SYMBOLS, default=FNO_SYMBOLS[:15], key="fno_custom_symbols")
        else:
            symbols = FNO_SYMBOLS
        max_symbols = st.slider("Max symbols to scan this run (perf control)", 10, len(FNO_SYMBOLS),
                                 min(60, len(FNO_SYMBOLS)), key="fno_max_symbols")
        symbols = symbols[:max_symbols]

        st.divider()
        auto_refresh_on = st.checkbox("🔁 Auto-refresh scan", value=False, key="fno_autorefresh_on")
        refresh_secs = st.number_input("Refresh interval (seconds)", min_value=30, value=180, step=30,
                                        disabled=not auto_refresh_on, key="fno_refresh_secs")
        run_scan = st.button("🚀 Run Full Scan", type="primary", use_container_width=True, key="fno_run_scan")

    if auto_refresh_on and AUTOREFRESH_AVAILABLE:
        st_autorefresh(interval=int(refresh_secs * 1000), key="fno_autorefresh_timer")
    elif auto_refresh_on and not AUTOREFRESH_AVAILABLE:
        st.sidebar.warning("Install `streamlit-autorefresh` to enable true auto-refresh.")

    if "fno_scan_df" not in st.session_state:
        st.session_state["fno_scan_df"] = pd.DataFrame()
        st.session_state["fno_last_scan_time"] = None

    should_scan = run_scan or (auto_refresh_on and AUTOREFRESH_AVAILABLE)

    if should_scan:
        progress_bar = st.progress(0.0, text="Starting scan...")

        def _cb(fraction, sym):
            progress_bar.progress(fraction, text=f"Scanning {sym} ({int(fraction*100)}%)")

        with st.spinner("Running institutional multi-factor scan across NSE F&O universe via Fyers..."):
            scan_df = run_full_scan(fyers, symbols, capital, risk_pct, progress_cb=_cb)

        progress_bar.empty()
        st.session_state["fno_scan_df"] = scan_df
        st.session_state["fno_last_scan_time"] = dt.datetime.now()

    scan_df = st.session_state["fno_scan_df"]
    last_time = st.session_state["fno_last_scan_time"]

    if scan_df is None or scan_df.empty:
        st.info("👈 Configure settings in the sidebar and click **Run Full Scan** to generate signals.")
        return

    if last_time:
        st.caption(f"Last scan: {last_time.strftime('%Y-%m-%d %H:%M:%S')} | "
                    f"Symbols scanned: {len(scan_df)} | Unique symbols: {scan_df['Symbol'].nunique()}")

    buys, sells = rank_and_split(scan_df)

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Total Scanned", len(scan_df))
    m2.metric("BUY Signals ≥60%", int((pd.to_numeric(scan_df["AI Confidence %"], errors="coerce") >= 60).sum()))
    m3.metric("Top BUY Confidence", f"{eng.safe_round(buys['AI Confidence %'].max()) if not buys.empty else 0}%")
    m4.metric("Top SELL Confidence", f"{eng.safe_round(sells['AI Confidence %'].max()) if not sells.empty else 0}%")

    st.divider()
    render_table_block("🟢 TOP 10 F&O BUY STOCKS", buys, "BUY", "buy")
    st.divider()
    render_table_block("🔴 TOP 10 F&O SELL STOCKS", sells, "SELL", "sell")

    st.divider()
    with st.expander("📄 View full raw scan (all symbols, all columns)"):
        st.dataframe(scan_df, use_container_width=True, height=500)
        st.download_button(
            "⬇️ Export Full Scan CSV", data=scan_df.to_csv(index=False).encode("utf-8"),
            file_name=f"fno_full_scan_{dt.date.today()}.csv", mime="text/csv", key="fno_full_csv",
        )
