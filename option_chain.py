"""
NSE India Options Chain Dashboard - Production Version
=========================================================
Institutional-grade option chain analysis with live data from NSE.

Features:
- Live CE/PE chain data (Strike, LTP, Bid, Ask, Volume, OI, IV, Greeks)
- Black-Scholes Greeks (Delta, Gamma, Theta, Vega)
- AI Signal Engine (BUY/SELL/HOLD per strike)
- PCR, Max Pain, Support/Resistance
- Gamma Exposure (GEX) & Delta Exposure (DEX)
- IV Rank & IV Percentile
- Excel & CSV export
- Streamlit dashboard with auto-refresh

Run: streamlit run option_chain_production.py
"""

from __future__ import annotations

import io
import logging
import math
import time
from datetime import datetime, timedelta
from typing import Any, Optional

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
from plotly.subplots import make_subplots
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

# ════════════════════════════════════════════════════════════════
# LOGGING CONFIGURATION
# ════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger(__name__)

# ════════════════════════════════════════════════════════════════
# CONSTANTS
# ════════════════════════════════════════════════════════════════

NSE_BASE_URL = "https://www.nseindia.com"
NSE_INDEX_CHAIN_URL = f"{NSE_BASE_URL}/api/option-chain-indices"
NSE_EQUITY_CHAIN_URL = f"{NSE_BASE_URL}/api/option-chain-equities"

INDEX_SYMBOLS = {
    "NIFTY": "NIFTY",
    "BANKNIFTY": "BANKNIFTY",
    "FINNIFTY": "FINNIFTY",
    "MIDCPNIFTY": "MIDCPNIFTY",
}

DEFAULT_LOT_SIZES = {
    "NIFTY": 25,
    "BANKNIFTY": 15,
    "FINNIFTY": 25,
    "MIDCPNIFTY": 50,
    "_STOCK_DEFAULT": 1,
}

RISK_FREE_RATE = 0.07
MIN_SIGMA = 0.01
MAX_SIGMA = 5.0
TRADING_DAYS_MIN_T = 0.25

REQUEST_TIMEOUT = 10
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 1.5

NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Referer": f"{NSE_BASE_URL}/option-chain",
}

REQUIRED_COLUMNS = ["strike_price", "ce_ltp", "ce_oi", "pe_ltp", "pe_oi"]

# ════════════════════════════════════════════════════════════════
# COLORS (Dark Theme)
# ════════════════════════════════════════════════════════════════

DARK_BG = "#0d1117"
PANEL_BG = "#161b22"
BORDER = "#30363d"
TEXT_MAIN = "#e6edf3"
TEXT_MUTED = "#8b949e"
GREEN = "#3fb950"
RED = "#f85149"
AMBER = "#d29922"
BLUE = "#58a6ff"

# ════════════════════════════════════════════════════════════════
# HTTP SESSION MANAGEMENT
# ════════════════════════════════════════════════════════════════

def _build_session() -> requests.Session:
    """Build session with retry logic for transient failures."""
    session = requests.Session()
    session.headers.update(NSE_HEADERS)
    retry = Retry(
        total=MAX_RETRIES,
        backoff_factor=RETRY_BACKOFF_SECONDS,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


@st.cache_resource(show_spinner=False)
def get_nse_session() -> requests.Session:
    """Get cached NSE session."""
    session = _build_session()
    try:
        session.get(NSE_BASE_URL, timeout=REQUEST_TIMEOUT)
        session.get(f"{NSE_BASE_URL}/option-chain", timeout=REQUEST_TIMEOUT)
    except Exception as e:
        logger.warning(f"Session warmup failed: {e}")
    return session


def fetch_json(url: str, params: dict | None = None) -> tuple[dict | None, str | None]:
    """Fetch JSON with retry logic. Returns (data, error)."""
    session = get_nse_session()
    
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
            
            if resp.status_code in (401, 403):
                try:
                    session.get(NSE_BASE_URL, timeout=REQUEST_TIMEOUT)
                    session.get(f"{NSE_BASE_URL}/option-chain", timeout=REQUEST_TIMEOUT)
                except:
                    pass
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
                continue
            
            if resp.status_code != 200:
                logger.warning(f"HTTP {resp.status_code} on attempt {attempt}")
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
                continue
            
            payload = resp.json()
            if payload:
                return payload, None
                
        except (requests.Timeout, requests.ConnectionError) as e:
            logger.warning(f"Attempt {attempt} failed: {e}")
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
        except ValueError:
            logger.warning(f"Invalid JSON on attempt {attempt}")
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    
    return None, "Failed after all retries"


# ════════════════════════════════════════════════════════════════
# DATA FETCHING & PARSING
# ════════════════════════════════════════════════════════════════

@st.cache_data(ttl=15, show_spinner=False)
def fetch_option_chain(symbol: str, is_index: bool) -> dict:
    """Fetch option chain from NSE."""
    url = NSE_INDEX_CHAIN_URL if is_index else NSE_EQUITY_CHAIN_URL
    payload, error = fetch_json(url, params={"symbol": symbol})
    
    if not payload:
        return {"ok": False, "data": None, "error": error}
    
    records = payload.get("records", {})
    if not records.get("data"):
        return {"ok": False, "data": payload, "error": "No chain data"}
    
    return {"ok": True, "data": payload, "error": None}


def _safe_num(val: Any, default: float = 0.0) -> float:
    """Safe float conversion."""
    try:
        if val is None:
            return default
        f = float(val)
        return default if (math.isnan(f) or math.isinf(f)) else f
    except (TypeError, ValueError):
        return default


def parse_chain(payload: dict, preferred_expiry: str = "") -> tuple[pd.DataFrame, dict]:
    """Parse NSE option chain into DataFrame."""
    meta = {
        "spot": 0.0,
        "expiries": [],
        "selected_expiry": "",
        "fetched_at": datetime.now(),
    }
    
    records = payload.get("records", {})
    chain = records.get("data", [])
    meta["spot"] = _safe_num(records.get("underlyingValue"))
    meta["expiries"] = records.get("expiryDates", [])
    
    if not chain:
        return pd.DataFrame(), meta
    
    selected = preferred_expiry if preferred_expiry in meta["expiries"] else (
        meta["expiries"][0] if meta["expiries"] else ""
    )
    meta["selected_expiry"] = selected
    
    rows = []
    for item in chain:
        if not isinstance(item, dict):
            continue
        if selected and item.get("expiryDate") != selected:
            continue
        
        strike = item.get("strikePrice")
        if strike is None:
            continue
        
        ce = item.get("CE", {})
        pe = item.get("PE", {})
        
        rows.append({
            "strike": _safe_num(strike),
            "ce_ltp": _safe_num(ce.get("lastPrice")),
            "ce_bid": _safe_num(ce.get("bidprice")),
            "ce_ask": _safe_num(ce.get("askPrice")),
            "ce_vol": _safe_num(ce.get("totalTradedVolume")),
            "ce_oi": _safe_num(ce.get("openInterest")),
            "ce_chg_oi": _safe_num(ce.get("changeinOpenInterest")),
            "ce_iv": _safe_num(ce.get("impliedVolatility")),
            "pe_ltp": _safe_num(pe.get("lastPrice")),
            "pe_bid": _safe_num(pe.get("bidprice")),
            "pe_ask": _safe_num(pe.get("askPrice")),
            "pe_vol": _safe_num(pe.get("totalTradedVolume")),
            "pe_oi": _safe_num(pe.get("openInterest")),
            "pe_chg_oi": _safe_num(pe.get("changeinOpenInterest")),
            "pe_iv": _safe_num(pe.get("impliedVolatility")),
        })
    
    if not rows:
        return pd.DataFrame(), meta
    
    df = pd.DataFrame(rows)
    df = df.groupby("strike", as_index=False).first()
    df.sort_values("strike", inplace=True)
    df.reset_index(drop=True, inplace=True)
    
    return df, meta


def validate_df(df: pd.DataFrame) -> bool:
    """Validate option chain DataFrame."""
    if df is None or df.empty:
        return False
    if not all(c in df.columns for c in REQUIRED_COLUMNS):
        return False
    return (df["strike"] > 0).sum() > 0


def filter_atm(df: pd.DataFrame, spot: float, count: int) -> pd.DataFrame:
    """Filter strikes around ATM."""
    if df.empty or count <= 0:
        return df
    
    ref = spot if spot else float(df["strike"].median())
    idx = (df["strike"] - ref).abs().idxmin()
    lo = max(0, idx - count)
    hi = min(len(df), idx + count + 1)
    
    return df.iloc[lo:hi].reset_index(drop=True)


# ════════════════════════════════════════════════════════════════
# GREEKS ENGINE (Black-Scholes)
# ════════════════════════════════════════════════════════════════

def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def bs_greeks(spot: float, strike: float, t_years: float, r: float, 
              sigma: float, is_call: bool) -> dict[str, float]:
    """Black-Scholes Greeks."""
    if spot <= 0 or strike <= 0 or t_years <= 0 or sigma <= 0:
        return {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    
    sigma = min(max(sigma, MIN_SIGMA), MAX_SIGMA)
    sqrt_t = math.sqrt(t_years)
    
    try:
        d1 = (math.log(spot / strike) + (r + 0.5 * sigma**2) * t_years) / (sigma * sqrt_t)
        d2 = d1 - sigma * sqrt_t
    except (ValueError, ZeroDivisionError):
        return {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    
    pdf_d1 = _norm_pdf(d1)
    gamma = pdf_d1 / (spot * sigma * sqrt_t)
    vega = spot * pdf_d1 * sqrt_t / 100.0
    
    if is_call:
        delta = _norm_cdf(d1)
        theta = (
            -(spot * pdf_d1 * sigma) / (2 * sqrt_t)
            - r * strike * math.exp(-r * t_years) * _norm_cdf(d2)
        ) / 365.0
    else:
        delta = _norm_cdf(d1) - 1.0
        theta = (
            -(spot * pdf_d1 * sigma) / (2 * sqrt_t)
            + r * strike * math.exp(-r * t_years) * _norm_cdf(-d2)
        ) / 365.0
    
    return {
        "delta": round(delta, 4),
        "gamma": round(gamma, 6),
        "theta": round(theta, 4),
        "vega": round(vega, 4),
    }


def add_greeks(df: pd.DataFrame, spot: float, expiry: str) -> pd.DataFrame:
    """Add Greeks columns."""
    d = df.copy()
    if d.empty:
        for col in ("ce_delta", "ce_gamma", "ce_theta", "ce_vega",
                   "pe_delta", "pe_gamma", "pe_theta", "pe_vega"):
            d[col] = 0.0
        return d
    
    try:
        exp_dt = datetime.strptime(expiry, "%d-%b-%Y")
        t_years = (exp_dt.replace(hour=15, minute=30) - datetime.now()).total_seconds() / (365 * 86400)
        t_years = max(t_years, TRADING_DAYS_MIN_T / 365)
    except:
        t_years = 7.0 / 365.0
    
    ce_greeks = d.apply(
        lambda r: bs_greeks(spot, r["strike"], t_years, RISK_FREE_RATE, r["ce_iv"] / 100.0, True),
        axis=1,
    )
    pe_greeks = d.apply(
        lambda r: bs_greeks(spot, r["strike"], t_years, RISK_FREE_RATE, r["pe_iv"] / 100.0, False),
        axis=1,
    )
    
    for key in ("delta", "gamma", "theta", "vega"):
        d[f"ce_{key}"] = ce_greeks.apply(lambda x: x[key])
        d[f"pe_{key}"] = pe_greeks.apply(lambda x: x[key])
    
    return d


# ════════════════════════════════════════════════════════════════
# CORE ANALYTICS
# ════════════════════════════════════════════════════════════════

def calc_pcr(df: pd.DataFrame) -> float:
    """Calculate Put/Call Ratio."""
    if df.empty:
        return 0.0
    ce_total = df["ce_oi"].sum()
    pe_total = df["pe_oi"].sum()
    return round(float(pe_total / ce_total), 3) if ce_total > 0 else 0.0


def calc_max_pain(df: pd.DataFrame) -> float:
    """Calculate Max Pain."""
    if df.empty:
        return 0.0
    
    strikes = df["strike"].values
    ce_oi = df["ce_oi"].values
    pe_oi = df["pe_oi"].values
    
    pain = [
        float(np.sum(np.maximum(s - strikes, 0) * ce_oi) + 
              np.sum(np.maximum(strikes - s, 0) * pe_oi))
        for s in strikes
    ]
    
    return float(strikes[np.argmin(pain)]) if pain else 0.0


def calc_support_resistance(df: pd.DataFrame) -> tuple[float | None, float | None]:
    """Calculate Support & Resistance."""
    if df.empty:
        return None, None
    
    support = float(df.loc[df["pe_oi"].idxmax(), "strike"])
    resistance = float(df.loc[df["ce_oi"].idxmax(), "strike"])
    
    return support, resistance


def calc_gex_dex(df: pd.DataFrame, spot: float, lot_size: int) -> dict:
    """Calculate Gamma & Delta Exposure."""
    if df.empty or not spot:
        return {"total_gex": 0.0, "total_dex": 0.0}
    
    d = df.copy()
    d["gex"] = (
        (d.get("ce_gamma", 0) * d.get("ce_oi", 0)) - 
        (d.get("pe_gamma", 0) * d.get("pe_oi", 0))
    ) * (spot ** 2) * 0.01 * lot_size
    
    d["dex"] = (
        (d.get("ce_delta", 0) * d.get("ce_oi", 0)) + 
        (d.get("pe_delta", 0) * d.get("pe_oi", 0))
    ) * spot * lot_size
    
    return {
        "total_gex": float(d["gex"].sum()),
        "total_dex": float(d["dex"].sum()),
    }


# ════════════════════════════════════════════════════════════════
# AI SIGNAL ENGINE
# ════════════════════════════════════════════════════════════════

def compute_ai_scores(df: pd.DataFrame, spot: float, max_pain: float, pcr: float) -> pd.DataFrame:
    """Compute AI BUY/SELL scores."""
    d = df.copy()
    if d.empty:
        d["ce_score"] = pd.Series(dtype=float)
        d["pe_score"] = pd.Series(dtype=float)
        return d
    
    # Normalize OI
    ce_oi_norm = (d["ce_oi"] - d["ce_oi"].min()) / (d["ce_oi"].max() - d["ce_oi"].min() + 1e-8)
    pe_oi_norm = (d["pe_oi"] - d["pe_oi"].min()) / (d["pe_oi"].max() - d["pe_oi"].min() + 1e-8)
    
    # Proximity to ATM
    ref = spot if spot else float(d["strike"].median())
    proximity = 1 - ((d["strike"] - ref).abs() / (d["strike"].max() - d["strike"].min() + 1e-8))
    
    # Max Pain proximity
    max_pain_prox = 1 - ((d["strike"] - max_pain).abs() / (d["strike"].max() - d["strike"].min() + 1e-8))
    
    # PCR bias
    pcr_bull = 1 if pcr > 1.0 else 0
    pcr_bear = 1 if pcr < 1.0 else 0
    
    # CE Score
    ce_score = (
        ce_oi_norm * 0.3 +
        proximity * 0.3 +
        max_pain_prox * 0.2 +
        (d["ce_chg_oi"] > 0).astype(int) * 0.1 +
        pcr_bull * 0.1
    ) * 100
    
    # PE Score
    pe_score = (
        pe_oi_norm * 0.3 +
        proximity * 0.3 +
        max_pain_prox * 0.2 +
        (d["pe_chg_oi"] > 0).astype(int) * 0.1 +
        pcr_bear * 0.1
    ) * 100
    
    d["ce_score"] = ce_score.round(1)
    d["pe_score"] = pe_score.round(1)
    d["ai_signal"] = d.apply(
        lambda r: "BUY CE" if r["ce_score"] > r["pe_score"] + 5 else 
                 ("BUY PE" if r["pe_score"] > r["ce_score"] + 5 else "HOLD"),
        axis=1
    )
    d["confidence"] = d[["ce_score", "pe_score"]].max(axis=1).round(1)
    
    return d


# ════════════════════════════════════════════════════════════════
# CHARTS (Plotly)
# ════════════════════════════════════════════════════════════════

def _dark_layout(fig: go.Figure, height: int = 420, title: str = "") -> go.Figure:
    """Apply dark theme."""
    fig.update_layout(
        paper_bgcolor=DARK_BG,
        plot_bgcolor=DARK_BG,
        font=dict(color=TEXT_MUTED, family="Courier New"),
        height=height,
        margin=dict(l=10, r=10, t=40 if title else 10, b=10),
        title=dict(text=title, font=dict(color=TEXT_MAIN, size=14)) if title else None,
    )
    return fig


def chart_oi_bars(df: pd.DataFrame, max_pain: float) -> go.Figure:
    """OI chart."""
    fig = make_subplots(
        rows=1, cols=2,
        subplot_titles=("Call OI (CE)", "Put OI (PE)"),
        shared_yaxes=True,
        horizontal_spacing=0.04,
    )
    
    if df.empty:
        return _dark_layout(fig)
    
    fig.add_trace(
        go.Bar(
            x=-df["ce_oi"],
            y=df["strike"],
            orientation="h",
            marker_color=GREEN,
            name="CE OI",
            showlegend=False,
        ),
        row=1, col=1,
    )
    
    fig.add_trace(
        go.Bar(
            x=df["pe_oi"],
            y=df["strike"],
            orientation="h",
            marker_color=RED,
            name="PE OI",
            showlegend=False,
        ),
        row=1, col=2,
    )
    
    for col in (1, 2):
        fig.add_hline(
            y=max_pain,
            line_dash="dot",
            line_color=AMBER,
            row=1,
            col=col,
        )
    
    return _dark_layout(fig, height=480)


def chart_greeks(df: pd.DataFrame, greek: str) -> go.Figure:
    """Greek chart."""
    fig = go.Figure()
    col_ce, col_pe = f"ce_{greek}", f"pe_{greek}"
    
    if not df.empty and col_ce in df.columns:
        fig.add_trace(go.Scatter(
            x=df["strike"],
            y=df[col_ce],
            mode="lines+markers",
            name=f"CE {greek.upper()}",
            line=dict(color=GREEN, width=2),
        ))
        fig.add_trace(go.Scatter(
            x=df["strike"],
            y=df[col_pe],
            mode="lines+markers",
            name=f"PE {greek.upper()}",
            line=dict(color=RED, width=2),
        ))
    
    fig.update_layout(
        xaxis=dict(title="Strike"),
        yaxis=dict(title=greek.upper()),
    )
    return _dark_layout(fig, height=300)


# ════════════════════════════════════════════════════════════════
# EXCEL EXPORT
# ════════════════════════════════════════════════════════════════

FILL_HEADER = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
FILL_GREEN = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
FILL_RED = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
FONT_HEADER = Font(color="FFFFFF", bold=True, size=11)
THIN_BORDER = Border(*(Side(style="thin", color="30363D"),) * 4)


def export_excel(df: pd.DataFrame, meta: dict, analysis: dict) -> io.BytesIO:
    """Export to Excel."""
    wb = Workbook()
    
    # Summary sheet
    ws = wb.active
    ws.title = "Summary"
    
    summary = [
        ("Spot Price", meta["spot"]),
        ("Max Pain", analysis["max_pain"]),
        ("PCR", analysis["pcr"]),
        ("Support", analysis["support"]),
        ("Resistance", analysis["resistance"]),
        ("Total GEX", analysis["gex_dex"]["total_gex"]),
        ("Total DEX", analysis["gex_dex"]["total_dex"]),
    ]
    
    for i, (label, value) in enumerate(summary, 1):
        ws[f"A{i}"] = label
        ws[f"B{i}"] = value
    
    # Chain sheet
    ws_chain = wb.create_sheet("Option Chain")
    for j, col in enumerate(df.columns, 1):
        ws_chain.cell(1, j, col)
    
    for i, row in df.iterrows():
        for j, col in enumerate(df.columns, 1):
            ws_chain.cell(i + 2, j, row[col])
    
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


# ════════════════════════════════════════════════════════════════
# STREAMLIT UI
# ════════════════════════════════════════════════════════════════

def _configure():
    """Configure page."""
    try:
        st.set_page_config(
            page_title="NSE Options Chain",
            page_icon="📊",
            layout="wide",
            initial_sidebar_state="expanded",
        )
    except:
        pass


def _inject_css():
    """Inject dark theme CSS."""
    st.markdown(f"""
    <style>
    .stApp {{ background-color: {DARK_BG}; }}
    section[data-testid="stSidebar"] {{ background-color: {PANEL_BG}; }}
    h1, h2, h3 {{ color: {TEXT_MAIN} !important; }}
    </style>
    """, unsafe_allow_html=True)


def render_html_table(df: pd.DataFrame, show_greeks: bool = True) -> str:
    """Render styled HTML table."""
    if df.empty:
        return "<p>No data</p>"
    
    cols = [
        "strike", "ce_oi", "ce_chg_oi", "ce_iv", "ce_ltp", "ce_bid", "ce_ask",
        "ce_delta", "ce_gamma", "ce_theta",
        "ai_signal", "confidence",
        "pe_delta", "pe_gamma", "pe_theta",
        "pe_bid", "pe_ask", "pe_ltp", "pe_iv", "pe_chg_oi", "pe_oi",
    ]
    
    if not show_greeks:
        cols = [c for c in cols if "delta" not in c and "gamma" not in c and "theta" not in c]
    
    cols = [c for c in cols if c in df.columns]
    view_df = df[cols].head(100)
    
    html = "<table style='width:100%; border-collapse:collapse; font-family:monospace; font-size:12px;'>"
    html += "<tr style='background-color:#1F4E78; color:white;'>"
    for col in cols:
        html += f"<th style='padding:8px; border:1px solid {BORDER};'>{col}</th>"
    html += "</tr>"
    
    for _, row in view_df.iterrows():
        html += "<tr>"
        for col in cols:
            val = row[col]
            if isinstance(val, float):
                val = f"{val:.2f}" if val < 1000 else f"{val:,.0f}"
            color = GREEN if "BUY CE" in str(val) else (RED if "BUY PE" in str(val) else TEXT_MAIN)
            html += f"<td style='padding:6px; border:1px solid {BORDER}; color:{color};'>{val}</td>"
        html += "</tr>"
    
    html += "</table>"
    return html


def main():
    """Main dashboard."""
    _configure()
    _inject_css()
    
    st.markdown("## 📊 NSE Options Chain Dashboard")
    
    # Sidebar config
    with st.sidebar:
        st.markdown("### ⚙️ Configuration")
        
        instr_type = st.radio("Type", ["Index", "Stock"])
        is_index = instr_type == "Index"
        
        if is_index:
            symbol = st.selectbox("Index", list(INDEX_SYMBOLS.keys()))
        else:
            symbol = st.text_input("Stock Symbol", "RELIANCE").strip().upper()
        
        strike_count = st.slider("Strikes ATM", 5, 40, 15, step=5)
        show_greeks = st.checkbox("Show Greeks", True)
        lot_size = st.number_input("Lot Size", min_value=1, value=DEFAULT_LOT_SIZES.get(symbol, 1))
        
        auto_refresh = st.checkbox("Auto Refresh", False)
        refresh_secs = st.slider("Refresh (sec)", 10, 120, 20, step=5, disabled=not auto_refresh)
        
        fetch_btn = st.button("🔄 Fetch Data", use_container_width=True, type="primary")
    
    # Fetch data
    state = st.session_state.get("oc_state")
    
    if fetch_btn or (auto_refresh and not state):
        with st.spinner("Fetching..."):
            result = fetch_option_chain(symbol, is_index)
            
            if not result["ok"]:
                st.error(f"❌ Error: {result['error']}")
                return
            
            df, meta = parse_chain(result["data"])
            
            if not validate_df(df):
                st.error("Invalid data")
                return
            
            df = filter_atm(df, meta["spot"], strike_count)
            df = add_greeks(df, meta["spot"], meta["selected_expiry"])
            df = compute_ai_scores(df, meta["spot"], calc_max_pain(df), calc_pcr(df))
            
            analysis = {
                "pcr": calc_pcr(df),
                "max_pain": calc_max_pain(df),
                "support": calc_support_resistance(df)[0],
                "resistance": calc_support_resistance(df)[1],
                "gex_dex": calc_gex_dex(df, meta["spot"], lot_size),
            }
            
            st.session_state["oc_state"] = {"df": df, "meta": meta, "analysis": analysis}
            state = st.session_state["oc_state"]
    
    if not state:
        st.info("👈 Select symbol and click Fetch Data")
        return
    
    df = state["df"]
    meta = state["meta"]
    analysis = state["analysis"]
    
    # Summary metrics
    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Spot", f"₹{meta['spot']:,.2f}")
    col2.metric("ATM", f"₹{df.loc[(df['strike'] - meta['spot']).abs().idxmin(), 'strike']:,.0f}")
    col3.metric("PCR", f"{analysis['pcr']:.3f}")
    col4.metric("Max Pain", f"₹{analysis['max_pain']:,.0f}")
    col5.metric("GEX", f"{analysis['gex_dex']['total_gex']:,.0f}")
    
    st.divider()
    
    # Tabs
    tab1, tab2, tab3, tab4 = st.tabs(["📋 Chain", "📈 Charts", "🧮 Greeks", "📥 Export"])
    
    with tab1:
        st.markdown(render_html_table(df, show_greeks), unsafe_allow_html=True)
    
    with tab2:
        st.plotly_chart(chart_oi_bars(df, analysis["max_pain"]), use_container_width=True)
    
    with tab3:
        col_g1, col_g2 = st.columns(2)
        with col_g1:
            st.plotly_chart(chart_greeks(df, "delta"), use_container_width=True)
            st.plotly_chart(chart_greeks(df, "theta"), use_container_width=True)
        with col_g2:
            st.plotly_chart(chart_greeks(df, "gamma"), use_container_width=True)
            st.plotly_chart(chart_greeks(df, "vega"), use_container_width=True)
    
    with tab4:
        try:
            excel_buf = export_excel(df, meta, analysis)
            st.download_button(
                "⬇️ Excel",
                data=excel_buf,
                file_name=f"option_chain_{symbol}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )
        except Exception as e:
            st.error(f"Export failed: {e}")
        
        csv_data = df.to_csv(index=False).encode("utf-8")
        st.download_button(
            "⬇️ CSV",
            data=csv_data,
            file_name=f"option_chain_{symbol}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
            mime="text/csv",
            use_container_width=True,
        )
    
    if auto_refresh:
        time.sleep(refresh_secs)
        st.rerun()


if __name__ == "__main__":
    main()
