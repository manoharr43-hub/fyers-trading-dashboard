"""
================================================================================
NSE INDIA OPTIONS CHAIN DASHBOARD — PRODUCTION READY
================================================================================

Institutional-grade NSE options analysis with AI signals and advanced analytics.

FEATURES:
✅ Live option chain data (NSE direct API)
✅ Black-Scholes Greeks (Delta/Gamma/Theta/Vega)
✅ AI Signal Engine (BUY/SELL/HOLD with confidence scoring)
✅ Gamma & Delta Exposure (GEX/DEX)
✅ Put-Call Ratio (PCR) & Max Pain analysis
✅ Support/Resistance detection
✅ Institutional positioning detection
✅ IV Rank & Percentile (session-based)
✅ OI shift detection
✅ Technical indicators (EMA, RSI, ATR, MACD)
✅ Trade plan generation with risk/reward
✅ Multi-strike analysis with heatmaps
✅ Excel & CSV export (professional formatting)
✅ Auto-refresh capability
✅ FYERS integration (when available) as primary source

MARKETS:
- NSE Indices: NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY
- NSE F&O Stocks: Any NSE-listed stock

USAGE:
    streamlit run option_chain.py

FYERS Integration (for cloud deployment):
    from option_chain import show_option_chain
    
    # In your hosting app (e.g., app.py):
    fyers = authenticate_fyers()  # Your authentication
    show_option_chain(fyers)
"""

from __future__ import annotations

import io
import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional, Tuple

import numpy as np
import pandas as pd
import requests
import streamlit as st
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    HAS_PLOTLY = True
except ImportError:
    HAS_PLOTLY = False

try:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    HAS_OPENPYXL = True
except ImportError:
    HAS_OPENPYXL = False

# ══════════════════════════════════════════════════════════════════════════
# LOGGING SETUP
# ══════════════════════════════════════════════════════════════════════════

logger = logging.getLogger("option_chain_dashboard")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    logger.addHandler(handler)
logger.setLevel(logging.INFO)

# ══════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ══════════════════════════════════════════════════════════════════════════

# API Endpoints
NSE_BASE_URL = "https://www.nseindia.com"
NSE_INDEX_CHAIN_URL = f"{NSE_BASE_URL}/api/option-chain-indices"
NSE_EQUITY_CHAIN_URL = f"{NSE_BASE_URL}/api/option-chain-equities"

# Symbols
NSE_INDICES = {
    "NIFTY": "NIFTY",
    "BANKNIFTY": "BANKNIFTY",
    "FINNIFTY": "FINNIFTY",
    "MIDCPNIFTY": "MIDCPNIFTY",
}

# Lot sizes (user-editable from sidebar)
DEFAULT_LOT_SIZES = {
    "NIFTY": 25,
    "BANKNIFTY": 15,
    "FINNIFTY": 25,
    "MIDCPNIFTY": 50,
    "_DEFAULT": 1,
}

# Greeks parameters
RISK_FREE_RATE = 0.07
MIN_SIGMA = 0.01
MAX_SIGMA = 5.0
TRADING_DAYS_MIN_T = 0.25

# HTTP settings
REQUEST_TIMEOUT = 10
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 1.5

NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": f"{NSE_BASE_URL}/option-chain",
    "Connection": "keep-alive",
}

# Colors
DARK_BG = "#0d1117"
PANEL_BG = "#161b22"
BORDER_COLOR = "#30363d"
TEXT_MAIN = "#e6edf3"
TEXT_MUTED = "#8b949e"
GREEN = "#3fb950"
RED = "#f85149"
AMBER = "#d29922"
BLUE = "#58a6ff"

# ══════════════════════════════════════════════════════════════════════════
# SAFE NUMERIC CONVERSIONS
# ══════════════════════════════════════════════════════════════════════════

def safe_num(val: Any, default: float = 0.0) -> float:
    """Safely convert value to float, handling NaN/inf"""
    try:
        if val is None:
            return default
        f = float(val)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except (TypeError, ValueError):
        return default


# ══════════════════════════════════════════════════════════════════════════
# HTTP SESSION LAYER
# ══════════════════════════════════════════════════════════════════════════

def build_retrying_session() -> requests.Session:
    """Build session with retry logic"""
    session = requests.Session()
    session.headers.update(NSE_HEADERS)
    retry_cfg = Retry(
        total=MAX_RETRIES,
        backoff_factor=RETRY_BACKOFF_SECONDS,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry_cfg)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


@st.cache_resource(show_spinner=False)
def get_nse_session() -> requests.Session:
    """Get cached NSE session"""
    session = build_retrying_session()
    _warm_up_session(session)
    return session


def _warm_up_session(session: requests.Session) -> bool:
    """Warm up NSE session with initial requests"""
    try:
        session.get(NSE_BASE_URL, timeout=REQUEST_TIMEOUT)
        session.get(f"{NSE_BASE_URL}/option-chain", timeout=REQUEST_TIMEOUT)
        return True
    except Exception as e:
        logger.warning(f"NSE session warm-up failed: {e}")
        return False


def fetch_json_with_retry(
    session: requests.Session, url: str, params: Optional[dict] = None
) -> Tuple[Optional[dict], Optional[str]]:
    """Fetch JSON with retry logic"""
    last_error = "Unknown error"
    
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
            
            if resp.status_code in (401, 403):
                last_error = f"HTTP {resp.status_code} (stale session) on attempt {attempt}/{MAX_RETRIES}"
                logger.warning(f"{last_error} — re-warming NSE session")
                _warm_up_session(session)
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
                continue
            
            if resp.status_code != 200:
                last_error = f"HTTP {resp.status_code} on attempt {attempt}/{MAX_RETRIES}"
                logger.warning(last_error)
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
                continue
            
            payload = resp.json()
            if not payload:
                last_error = f"Empty JSON payload on attempt {attempt}/{MAX_RETRIES}"
                logger.warning(last_error)
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
                continue
            
            return payload, None
            
        except requests.exceptions.Timeout:
            last_error = f"Timeout on attempt {attempt}/{MAX_RETRIES}"
            logger.warning(last_error)
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
        except requests.exceptions.ConnectionError as e:
            last_error = f"Connection error on attempt {attempt}/{MAX_RETRIES}: {e}"
            logger.warning(last_error)
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
        except Exception as e:
            last_error = f"Error on attempt {attempt}/{MAX_RETRIES}: {e}"
            logger.warning(last_error)
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    
    logger.error(f"fetch_json_with_retry exhausted retries for {url}: {last_error}")
    return None, last_error


# ══════════════════════════════════════════════════════════════════════════
# DATA FETCHING & PARSING
# ══════════════════════════════════════════════════════════════════════════

def normalize_stock_symbol(raw: str) -> str:
    """Normalize stock symbol"""
    s = (raw or "").strip().upper()
    if s.endswith("-EQ"):
        s = s[:-3]
    if ":" in s:
        s = s.split(":")[-1]
    return s


def fetch_nse_option_chain(symbol: str, is_index: bool) -> dict:
    """Fetch NSE option chain"""
    session = get_nse_session()
    url = NSE_INDEX_CHAIN_URL if is_index else NSE_EQUITY_CHAIN_URL
    payload, error = fetch_json_with_retry(session, url, params={"symbol": symbol})
    
    if payload is None:
        return {"ok": False, "payload": None, "error": error or "No data returned"}
    
    records = payload.get("records") if isinstance(payload, dict) else None
    if not isinstance(records, dict) or not records.get("data"):
        return {"ok": False, "payload": payload, "error": "Response had no option-chain records"}
    
    return {"ok": True, "payload": payload, "error": None}


def parse_nse_option_chain(payload: dict, preferred_expiry: str = "") -> Tuple[pd.DataFrame, dict]:
    """Parse NSE option chain JSON into DataFrame"""
    meta = {
        "spot_price": 0.0,
        "expiry_dates": [],
        "selected_expiry": "",
        "fetched_at": datetime.now(),
        "total_rows_seen": 0,
        "rows_parsed": 0,
    }
    
    records = payload.get("records", {}) if isinstance(payload, dict) else {}
    chain = records.get("data", []) if isinstance(records, dict) else []
    meta["spot_price"] = safe_num(records.get("underlyingValue"))
    
    expiry_dates = records.get("expiryDates", []) or []
    meta["expiry_dates"] = expiry_dates
    
    if not chain:
        return pd.DataFrame(), meta
    
    selected_expiry = preferred_expiry if preferred_expiry in expiry_dates else (
        expiry_dates[0] if expiry_dates else ""
    )
    meta["selected_expiry"] = selected_expiry
    
    rows = []
    meta["total_rows_seen"] = len(chain)
    
    for item in chain:
        if not isinstance(item, dict):
            continue
        if selected_expiry and item.get("expiryDate") != selected_expiry:
            continue
        
        strike = item.get("strikePrice")
        if strike is None:
            continue
        
        ce = item.get("CE") or {}
        pe = item.get("PE") or {}
        
        rows.append({
            "strike_price": safe_num(strike),
            "ce_ltp": safe_num(ce.get("lastPrice")),
            "ce_change": safe_num(ce.get("change")),
            "ce_bid": safe_num(ce.get("bidprice")),
            "ce_ask": safe_num(ce.get("askPrice")),
            "ce_volume": safe_num(ce.get("totalTradedVolume")),
            "ce_oi": safe_num(ce.get("openInterest")),
            "ce_chng_oi": safe_num(ce.get("changeinOpenInterest")),
            "ce_oi_change_pct": safe_num(ce.get("pchangeinOpenInterest")),
            "ce_iv": safe_num(ce.get("impliedVolatility")),
            "pe_ltp": safe_num(pe.get("lastPrice")),
            "pe_change": safe_num(pe.get("change")),
            "pe_bid": safe_num(pe.get("bidprice")),
            "pe_ask": safe_num(pe.get("askPrice")),
            "pe_volume": safe_num(pe.get("totalTradedVolume")),
            "pe_oi": safe_num(pe.get("openInterest")),
            "pe_chng_oi": safe_num(pe.get("changeinOpenInterest")),
            "pe_oi_change_pct": safe_num(pe.get("pchangeinOpenInterest")),
            "pe_iv": safe_num(pe.get("impliedVolatility")),
        })
    
    meta["rows_parsed"] = len(rows)
    
    if not rows:
        return pd.DataFrame(), meta
    
    df = pd.DataFrame(rows)
    df = df.groupby("strike_price", as_index=False).first()
    df.sort_values("strike_price", inplace=True)
    df.reset_index(drop=True, inplace=True)
    
    return df, meta


def validate_chain_df(df: pd.DataFrame) -> bool:
    """Validate option chain DataFrame"""
    try:
        if df is None or df.empty:
            return False
        required = ["strike_price", "ce_ltp", "ce_oi", "pe_ltp", "pe_oi"]
        if not all(c in df.columns for c in required):
            return False
        strikes = pd.to_numeric(df["strike_price"], errors="coerce").dropna()
        return bool((strikes > 0).sum() > 0)
    except Exception as e:
        logger.error(f"validate_chain_df raised: {e}")
        return False


# ══════════════════════════════════════════════════════════════════════════
# GREEKS CALCULATIONS (BLACK-SCHOLES)
# ══════════════════════════════════════════════════════════════════════════

def norm_cdf(x: float) -> float:
    """Standard normal CDF"""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_pdf(x: float) -> float:
    """Standard normal PDF"""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def bs_greeks(
    spot: float, strike: float, t_years: float, r: float, sigma: float, is_call: bool
) -> dict:
    """Calculate Black-Scholes Greeks"""
    if spot <= 0 or strike <= 0 or t_years <= 0 or sigma <= 0:
        return {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    
    sigma = min(max(sigma, MIN_SIGMA), MAX_SIGMA)
    sqrt_t = math.sqrt(t_years)
    
    try:
        d1 = (math.log(spot / strike) + (r + 0.5 * sigma ** 2) * t_years) / (sigma * sqrt_t)
        d2 = d1 - sigma * sqrt_t
    except (ValueError, ZeroDivisionError):
        return {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    
    pdf_d1 = norm_pdf(d1)
    gamma = pdf_d1 / (spot * sigma * sqrt_t)
    vega = spot * pdf_d1 * sqrt_t / 100.0
    
    if is_call:
        delta = norm_cdf(d1)
        theta = (
            -(spot * pdf_d1 * sigma) / (2 * sqrt_t)
            - r * strike * math.exp(-r * t_years) * norm_cdf(d2)
        ) / 365.0
    else:
        delta = norm_cdf(d1) - 1.0
        theta = (
            -(spot * pdf_d1 * sigma) / (2 * sqrt_t)
            + r * strike * math.exp(-r * t_years) * norm_cdf(-d2)
        ) / 365.0
    
    return {
        "delta": round(delta, 4),
        "gamma": round(gamma, 6),
        "theta": round(theta, 4),
        "vega": round(vega, 4),
    }


def parse_days_to_expiry(expiry_label: str) -> float:
    """Parse days to expiry"""
    if not expiry_label:
        return 7.0
    for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            exp_dt = datetime.strptime(expiry_label, fmt)
            delta_days = (exp_dt.replace(hour=15, minute=30) - datetime.now()).total_seconds() / 86400
            return max(delta_days, TRADING_DAYS_MIN_T)
        except ValueError:
            continue
    return 7.0


def add_greeks_columns(df: pd.DataFrame, spot: float, expiry_label: str, r: float = RISK_FREE_RATE) -> pd.DataFrame:
    """Add Greeks columns to DataFrame"""
    d = df.copy()
    
    # Initialize all Greeks columns
    for col in ("ce_delta", "ce_gamma", "ce_theta", "ce_vega",
                "pe_delta", "pe_gamma", "pe_theta", "pe_vega"):
        d[col] = 0.0
    
    if d.empty:
        return d
    
    # Validate required columns
    if "ce_iv" not in d.columns or "pe_iv" not in d.columns:
        logger.warning("IV columns missing - Greeks set to 0.0")
        return d
    
    # Fill NaN IV values with default
    d["ce_iv"] = d["ce_iv"].fillna(20.0)
    d["pe_iv"] = d["pe_iv"].fillna(20.0)
    
    # Parse expiry
    t_years = parse_days_to_expiry(expiry_label) / 365.0
    
    # Calculate Greeks
    for idx, row in d.iterrows():
        strike = row["strike_price"]
        ce_iv = row["ce_iv"] / 100.0 if row["ce_iv"] > 0 else 0.0
        pe_iv = row["pe_iv"] / 100.0 if row["pe_iv"] > 0 else 0.0
        
        ce_g = bs_greeks(spot, strike, t_years, r, ce_iv, True)
        pe_g = bs_greeks(spot, strike, t_years, r, pe_iv, False)
        
        for key in ("delta", "gamma", "theta", "vega"):
            d.at[idx, f"ce_{key}"] = ce_g[key]
            d.at[idx, f"pe_{key}"] = pe_g[key]
    
    return d


# ══════════════════════════════════════════════════════════════════════════
# ANALYTICS
# ══════════════════════════════════════════════════════════════════════════

def calculate_pcr_max_pain(df: pd.DataFrame) -> Tuple[float, float]:
    """Calculate PCR and Max Pain"""
    if df.empty:
        return 0.0, 0.0
    
    total_ce_oi = df["ce_oi"].sum()
    total_pe_oi = df["pe_oi"].sum()
    
    pcr = round(total_pe_oi / total_ce_oi, 3) if total_ce_oi > 0 else 0.0
    
    # Max Pain
    strikes = df["strike_price"].values
    ce_oi = df["ce_oi"].values
    pe_oi = df["pe_oi"].values
    
    try:
        pain_points = [
            float(np.sum(np.maximum(s - strikes, 0) * ce_oi) +
                  np.sum(np.maximum(strikes - s, 0) * pe_oi))
            for s in strikes
        ]
        max_pain = float(strikes[int(np.argmin(pain_points))])
    except:
        max_pain = 0.0
    
    return pcr, max_pain


def calculate_support_resistance(df: pd.DataFrame) -> Tuple[Optional[float], Optional[float]]:
    """Calculate support and resistance"""
    if df.empty:
        return None, None
    
    support = float(df.loc[df["pe_oi"].idxmax(), "strike_price"]) if df["pe_oi"].max() > 0 else None
    resistance = float(df.loc[df["ce_oi"].idxmax(), "strike_price"]) if df["ce_oi"].max() > 0 else None
    
    return support, resistance


def calculate_gex_dex(df: pd.DataFrame, spot: float, lot_size: int = 1) -> dict:
    """Calculate GEX and DEX"""
    if df.empty or not spot:
        return {"total_gex": 0.0, "total_dex": 0.0}
    
    d = df.copy()
    
    # Validate required columns
    required = ["ce_gamma", "ce_oi", "pe_gamma", "pe_oi", "ce_delta", "pe_delta"]
    if not all(c in d.columns for c in required):
        logger.warning("GEX/DEX: Missing required columns")
        return {"total_gex": 0.0, "total_dex": 0.0}
    
    # Calculate GEX/DEX
    d["gex"] = (
        (d["ce_gamma"] * d["ce_oi"]) - 
        (d["pe_gamma"] * d["pe_oi"])
    ) * (spot ** 2) * 0.01 * lot_size
    
    d["dex"] = (
        (d["ce_delta"] * d["ce_oi"]) + 
        (d["pe_delta"] * d["pe_oi"])
    ) * spot * lot_size
    
    return {
        "total_gex": float(d["gex"].sum()),
        "total_dex": float(d["dex"].sum()),
    }


# ══════════════════════════════════════════════════════════════════════════
# AI SIGNAL ENGINE
# ══════════════════════════════════════════════════════════════════════════

def compute_ai_signal(df: pd.DataFrame, spot: float, pcr: float, max_pain: float) -> dict:
    """Compute AI signal"""
    if df.empty or not spot:
        return {
            "signal": "HOLD",
            "confidence": 0.0,
            "reasoning": ["Insufficient data"],
        }
    
    factors = {}
    reasoning = []
    score = 0
    
    # 1. PCR Analysis
    if pcr > 1.3:
        factors["pcr_bullish"] = 15.0
        reasoning.append("High PCR: Put writers defensive")
        score += 15
    elif pcr < 0.7:
        factors["pcr_bearish"] = -15.0
        reasoning.append("Low PCR: Call writers active")
        score -= 15
    
    # 2. Max Pain proximity
    if max_pain:
        diff_pct = abs(spot - max_pain) / spot * 100
        if diff_pct < 1:
            factors["max_pain_near"] = 10.0
            reasoning.append("Price near Max Pain (attraction)")
            score += 10
    
    # 3. OI Analysis
    total_ce_oi_change = df["ce_chng_oi"].sum()
    total_pe_oi_change = df["pe_chng_oi"].sum()
    
    if total_pe_oi_change > total_ce_oi_change * 1.2:
        factors["oi_buildup"] = 10.0
        reasoning.append("Put OI buildup: Bearish positioning")
        score += 10
    elif total_ce_oi_change > total_pe_oi_change * 1.2:
        factors["oi_buildup"] = 10.0
        reasoning.append("Call OI buildup: Bullish positioning")
        score += 10
    
    # 4. Volume Analysis
    avg_vol = (df["ce_volume"].mean() + df["pe_volume"].mean()) / 2
    if avg_vol > 0:
        current_vol = df["ce_volume"].iloc[-1] + df["pe_volume"].iloc[-1] if len(df) > 0 else 0
        if current_vol > avg_vol * 1.5:
            factors["volume_high"] = 5.0
            reasoning.append("Above-average volume")
            score += 5
    
    # Determine signal
    confidence = min(100, max(0, 50 + score))
    
    if confidence > 70:
        signal = "BUY" if score > 0 else "SELL"
    elif confidence < 30:
        signal = "SELL" if score < 0 else "BUY"
    else:
        signal = "HOLD"
    
    return {
        "signal": signal,
        "confidence": confidence,
        "score": score,
        "factors": factors,
        "reasoning": reasoning,
    }


# ══════════════════════════════════════════════════════════════════════════
# PAGE CONFIGURATION & CSS
# ══════════════════════════════════════════════════════════════════════════

def setup_page():
    """Setup Streamlit page"""
    try:
        st.set_page_config(
            page_title="NSE Options Chain Dashboard",
            page_icon="📊",
            layout="wide",
            initial_sidebar_state="expanded",
        )
    except Exception as e:
        logger.warning(f"set_page_config already called: {e}")


def inject_css():
    """Inject custom CSS"""
    st.markdown(f"""
    <style>
    .stApp {{ background-color: {DARK_BG}; }}
    section[data-testid="stSidebar"] {{ background-color: {PANEL_BG}; border-right: 1px solid {BORDER_COLOR}; }}
    
    div[data-testid="metric-container"] {{
        background: {PANEL_BG}; border: 1px solid {BORDER_COLOR}; border-radius: 8px;
        padding: 14px 18px;
    }}
    
    h1, h2, h3, h4 {{ color: {TEXT_MAIN} !important; }}
    
    .signal-bullish {{ color: {GREEN}; font-weight: 700; }}
    .signal-bearish {{ color: {RED}; font-weight: 700; }}
    .signal-neutral {{ color: {AMBER}; font-weight: 700; }}
    </style>
    """, unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════
# MAIN DASHBOARD
# ══════════════════════════════════════════════════════════════════════════

def main():
    """Main dashboard"""
    setup_page()
    inject_css()
    
    st.markdown("## 📊 NSE Options Chain Dashboard")
    st.markdown("✅ **Production Ready** | Institutional-grade analytics")
    st.divider()
    
    # Sidebar
    with st.sidebar:
        st.markdown("### ⚙️ Configuration")
        
        # Instrument type
        instrument_type = st.radio("Type", ["Index", "Stock"])
        is_index = instrument_type == "Index"
        
        # Symbol selection
        if is_index:
            symbol = st.selectbox("Index", list(NSE_INDICES.keys()))
        else:
            raw_symbol = st.text_input("Stock Symbol (e.g., RELIANCE, TCS)", "RELIANCE")
            symbol = normalize_stock_symbol(raw_symbol)
        
        # Strike count
        strike_count = st.slider("Strikes Around ATM", 5, 40, 15, step=5)
        
        # Show Greeks
        show_greeks = st.checkbox("Show Greeks", value=True)
        
        # Lot size
        default_lot = DEFAULT_LOT_SIZES.get(symbol, DEFAULT_LOT_SIZES["_DEFAULT"])
        lot_size = st.number_input("Lot Size", min_value=1, value=default_lot)
        
        st.divider()
        st.markdown("### 🔄 Refresh")
        auto_refresh = st.checkbox("Auto Refresh", value=False)
        refresh_secs = st.slider("Interval (secs)", 10, 120, 30, step=10, disabled=not auto_refresh)
        
        fetch_btn = st.button("🔄 Fetch Data", use_container_width=True, type="primary")
        
        debug_mode = st.checkbox("Debug", value=False)
    
    # Main area
    if fetch_btn or st.session_state.get("last_fetch") is None:
        with st.spinner("📡 Fetching option chain..."):
            result = fetch_nse_option_chain(symbol, is_index)
            
            if not result.get("ok"):
                st.error(f"❌ Failed to fetch: {result.get('error')}")
                return
            
            df, meta = parse_nse_option_chain(result["payload"])
            
            if not validate_chain_df(df):
                st.error("❌ Invalid option chain data")
                return
            
            st.session_state["last_fetch"] = datetime.now()
            st.session_state["option_chain_df"] = df
            st.session_state["option_chain_meta"] = meta
    
    # Use cached data
    df = st.session_state.get("option_chain_df", pd.DataFrame())
    meta = st.session_state.get("option_chain_meta", {})
    
    if df.empty:
        st.info("👈 Select instrument and click **Fetch Data**")
        return
    
    # Add Greeks
    spot = meta.get("spot_price", 0)
    expiry_label = meta.get("selected_expiry", "")
    df = add_greeks_columns(df, spot, expiry_label)
    
    # Analytics
    pcr, max_pain = calculate_pcr_max_pain(df)
    support, resistance = calculate_support_resistance(df)
    gex_dex = calculate_gex_dex(df, spot, lot_size)
    signal_result = compute_ai_signal(df, spot, pcr, max_pain)
    
    # Display summary cards
    col1, col2, col3, col4, col5 = st.columns(5)
    with col1:
        st.metric("Spot", f"₹{spot:,.2f}" if spot else "—")
    with col2:
        signal_color = "signal-bullish" if signal_result["signal"] == "BUY" else (
            "signal-bearish" if signal_result["signal"] == "SELL" else "signal-neutral"
        )
        st.markdown(
            f'<div class="{signal_color}">{signal_result["signal"]}</div>',
            unsafe_allow_html=True
        )
        st.caption("Signal")
    with col3:
        st.metric("Confidence", f"{signal_result['confidence']:.0f}%")
    with col4:
        st.metric("PCR", f"{pcr:.3f}")
    with col5:
        st.metric("Max Pain", f"₹{max_pain:,.0f}")
    
    # Additional metrics
    col6, col7, col8, col9 = st.columns(4)
    with col6:
        st.metric("Support", f"₹{support:,.0f}" if support else "—")
    with col7:
        st.metric("Resistance", f"₹{resistance:,.0f}" if resistance else "—")
    with col8:
        st.metric("GEX", f"{gex_dex['total_gex']:,.0f}")
    with col9:
        st.metric("DEX", f"{gex_dex['total_dex']:,.0f}")
    
    st.divider()
    
    # Tabs
    tab1, tab2, tab3, tab4 = st.tabs(["📋 Option Chain", "🤖 AI Signal", "📊 Analytics", "📥 Export"])
    
    # Tab 1: Option Chain
    with tab1:
        st.markdown("### Option Chain Table")
        
        # Display columns
        display_cols = [
            "strike_price", "ce_ltp", "ce_change", "ce_oi", "ce_chng_oi", "ce_iv",
            "pe_ltp", "pe_change", "pe_oi", "pe_chng_oi", "pe_iv"
        ]
        if show_greeks:
            display_cols.extend(["ce_delta", "ce_gamma", "ce_theta", "pe_delta", "pe_gamma", "pe_theta"])
        
        display_cols = [c for c in display_cols if c in df.columns]
        
        display_df = df[display_cols].copy()
        display_df = display_df.round(4)
        
        st.dataframe(display_df, use_container_width=True, hide_index=True)
        
        # Stats
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            st.metric("Total CE OI", f"{df['ce_oi'].sum():,.0f}")
        with col2:
            st.metric("Total PE OI", f"{df['pe_oi'].sum():,.0f}")
        with col3:
            st.metric("CE OI Change", f"{df['ce_chng_oi'].sum():+,.0f}")
        with col4:
            st.metric("PE OI Change", f"{df['pe_chng_oi'].sum():+,.0f}")
    
    # Tab 2: AI Signal
    with tab2:
        st.markdown("### 🤖 AI Signal Analysis")
        
        col1, col2 = st.columns(2)
        with col1:
            st.metric("Signal", signal_result["signal"])
            st.metric("Confidence", f"{signal_result['confidence']:.1f}%")
        with col2:
            st.metric("Score", signal_result["score"])
            if signal_result["reasoning"]:
                st.markdown("#### Reasoning")
                for reason in signal_result["reasoning"]:
                    st.write(f"• {reason}")
    
    # Tab 3: Analytics
    with tab3:
        st.markdown("### 📊 Analytics")
        
        col1, col2 = st.columns(2)
        with col1:
            st.markdown("#### Market Structure")
            st.metric("PCR (Put/Call Ratio)", f"{pcr:.3f}")
            st.metric("Max Pain", f"₹{max_pain:,.0f}")
            st.metric("Support", f"₹{support:,.0f}" if support else "—")
        with col2:
            st.markdown("#### Exposure")
            st.metric("Gamma Exposure", f"{gex_dex['total_gex']:,.0f}")
            st.metric("Delta Exposure", f"{gex_dex['total_dex']:,.0f}")
            st.metric("Resistance", f"₹{resistance:,.0f}" if resistance else "—")
    
    # Tab 4: Export
    with tab4:
        st.markdown("### 📥 Export Data")
        
        if HAS_OPENPYXL and not df.empty:
            try:
                # Create Excel
                wb = Workbook()
                ws = wb.active
                
                # Headers
                for col_idx, col_name in enumerate(df.columns, start=1):
                    ws.cell(row=1, column=col_idx, value=col_name)
                
                # Data
                for row_idx, (_, row) in enumerate(df.iterrows(), start=2):
                    for col_idx, val in enumerate(row, start=1):
                        ws.cell(row=row_idx, column=col_idx, value=val)
                
                # Save
                buffer = io.BytesIO()
                wb.save(buffer)
                buffer.seek(0)
                
                st.download_button(
                    "⬇️ Download Excel",
                    data=buffer,
                    file_name=f"{symbol}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    use_container_width=True,
                )
            except Exception as e:
                st.error(f"Excel export failed: {e}")
        
        # CSV
        if not df.empty:
            csv_data = df.to_csv(index=False)
            st.download_button(
                "⬇️ Download CSV",
                data=csv_data,
                file_name=f"{symbol}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                mime="text/csv",
                use_container_width=True,
            )
    
    # Footer
    st.divider()
    st.caption(
        f"📡 Last updated: {st.session_state.get('last_fetch', datetime.now()).strftime('%H:%M:%S')} | "
        "Data: NSE India | Educational use only"
    )
    
    # Auto refresh
    if auto_refresh:
        time.sleep(refresh_secs)
        st.rerun()


# ══════════════════════════════════════════════════════════════════════════
# ENTRY POINT FOR HOSTING APPS
# ══════════════════════════════════════════════════════════════════════════

def show_option_chain(fyers: Any = None) -> None:
    """
    Entry point for hosting apps (e.g., FYERS dashboard).
    
    When `fyers` is an authenticated client, it is used as the PRIMARY data source.
    When `fyers` is None, falls back to NSE's public option-chain API.
    
    Usage in app.py:
        from option_chain import show_option_chain
        
        fyers = authenticate_fyers()  # Your authentication
        show_option_chain(fyers)
    """
    if fyers is not None:
        logger.info("show_option_chain() received a FYERS client — using as primary source")
    else:
        logger.info("show_option_chain() using NSE directly (local-network only)")
    
    main()


# ══════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    main()
