"""
option_chain.py
================
Institutional-grade NSE India Options Chain Dashboard.

Covers NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY and any NSE F&O stock, using
NSE India's public option-chain endpoints directly (no broker/API-key
dependency, so this file runs standalone).

Feature set:
    - Live CE/PE chain: Strike, LTP, Bid, Ask, Volume, OI, OI Change,
      OI Change %, IV, Delta, Gamma, Theta, Vega
    - AI Engine: BUY / SELL / HOLD per strike, Institutional Signal,
      Smart Money detection, Long/Short Buildup, Long/Short Unwinding,
      Call/Put Writing, Call/Put Unwinding, PCR, Max Pain, Max OI,
      OI Shift Detection
    - Greeks Engine: Black-Scholes Delta/Gamma/Theta/Vega, IV Rank,
      IV Percentile (session-based history), Gamma Exposure (GEX),
      Delta Exposure (DEX)
    - Intraday AI: Support & Resistance, ATM/ITM/OTM classification,
      Breakout / Reversal / Trend probability, Scalping & Swing signal
    - Dashboard: Streamlit UI, summary cards, color-coded/heatmapped
      chain table, Plotly charts, auto-refresh, filters, symbol search,
      expiry selection
    - Reports: Excel export (openpyxl, conditional formatting, auto
      column width) and CSV export
    - Robust error handling: retry logic, timeout handling, missing/NaN
      data handling, empty-response handling, structured logging
    - Performance: st.cache_data / st.cache_resource, vectorized pandas

Run with:
    streamlit run option_chain.py
"""

from __future__ import annotations

import io
import logging
import math
import time
from dataclasses import dataclass, field
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

# ══════════════════════════════════════════════════════════════════════════
# 1. LOGGING
# ══════════════════════════════════════════════════════════════════════════

logger = logging.getLogger("option_chain_dashboard")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    logger.addHandler(_handler)
logger.setLevel(logging.INFO)


# ══════════════════════════════════════════════════════════════════════════
# 2. CONSTANTS
# ══════════════════════════════════════════════════════════════════════════

NSE_BASE_URL = "https://www.nseindia.com"
NSE_INDEX_CHAIN_URL = f"{NSE_BASE_URL}/api/option-chain-indices"
NSE_EQUITY_CHAIN_URL = f"{NSE_BASE_URL}/api/option-chain-equities"

INDEX_SYMBOLS: dict[str, str] = {
    "NIFTY": "NIFTY",
    "BANKNIFTY": "BANKNIFTY",
    "FINNIFTY": "FINNIFTY",
    "MIDCPNIFTY": "MIDCPNIFTY",
}

# Default lot sizes used only as a starting point for GEX/DEX & notional
# calculations. NSE revises lot sizes periodically (quarterly review), so
# these are editable from the sidebar rather than trusted blindly.
DEFAULT_LOT_SIZES: dict[str, int] = {
    "NIFTY": 25,
    "BANKNIFTY": 15,
    "FINNIFTY": 25,
    "MIDCPNIFTY": 50,
    "_STOCK_DEFAULT": 1,
}

RISK_FREE_RATE = 0.07  # annualized, used only as a Black-Scholes input
MIN_SIGMA = 0.01
MAX_SIGMA = 5.0
TRADING_DAYS_MIN_T = 0.25  # floor of 6 hours expressed in days, avoids T=0

REQUEST_TIMEOUT = 10
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 1.5

_NSE_HEADERS = {
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

REQUIRED_CHAIN_COLUMNS = ["strike_price", "ce_ltp", "ce_oi", "pe_ltp", "pe_oi"]

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
# 3. HTTP / SESSION LAYER  (retry logic + timeout handling)
# ══════════════════════════════════════════════════════════════════════════

def _build_retrying_session() -> requests.Session:
    """Build a requests.Session with connection-level retry (urllib3 Retry)
    for transient network errors, on top of which fetch_json_with_retry()
    adds an application-level retry loop for NSE's anti-bot / cookie
    quirks (401s that resolve after a fresh warm-up)."""
    session = requests.Session()
    session.headers.update(_NSE_HEADERS)
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
    """One warmed-up session per Streamlit server process. Cached as a
    resource (not data) since requests.Session objects are not picklable
    in a meaningful way and must be reused, not recreated, per refresh."""
    session = _build_retrying_session()
    _warm_up_session(session)
    return session


def _warm_up_session(session: requests.Session) -> bool:
    """NSE requires a same-session cookie obtained by first hitting the
    website itself before the JSON API will respond with data (otherwise
    it returns 401/403). Never raises — a failed warm-up degrades to a
    later fetch failure that is itself handled gracefully."""
    try:
        session.get(NSE_BASE_URL, timeout=REQUEST_TIMEOUT)
        session.get(f"{NSE_BASE_URL}/option-chain", timeout=REQUEST_TIMEOUT)
        return True
    except requests.exceptions.RequestException as e:
        logger.warning("NSE session warm-up failed (will retry on next fetch): %s", e)
        return False


def fetch_json_with_retry(
    session: requests.Session, url: str, params: Optional[dict] = None,
    max_retries: int = MAX_RETRIES,
) -> tuple[Optional[dict], Optional[str]]:
    """Fetches JSON with an application-level retry loop. Returns
    (payload, error_message) — payload is None on failure, with a
    human-readable error_message explaining why. Handles: connection
    errors, timeouts, non-200 status, invalid/empty JSON, and NSE's
    occasional stale-cookie 401 (recovered via a fresh warm-up + retry)."""
    last_error = "Unknown error"
    for attempt in range(1, max_retries + 1):
        try:
            resp = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
        except requests.exceptions.Timeout:
            last_error = f"Timeout on attempt {attempt}/{max_retries}"
            logger.warning("%s for %s", last_error, url)
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
            continue
        except requests.exceptions.ConnectionError as e:
            last_error = f"Connection error on attempt {attempt}/{max_retries}: {e}"
            logger.warning(last_error)
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
            continue
        except requests.exceptions.RequestException as e:
            last_error = f"Request exception on attempt {attempt}/{max_retries}: {e}"
            logger.warning(last_error)
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
            continue

        if resp.status_code in (401, 403):
            last_error = f"HTTP {resp.status_code} (stale session) on attempt {attempt}/{max_retries}"
            logger.warning("%s — re-warming NSE session and retrying", last_error)
            _warm_up_session(session)
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
            continue

        if resp.status_code != 200:
            last_error = f"HTTP {resp.status_code} on attempt {attempt}/{max_retries}"
            logger.warning(last_error)
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
            continue

        try:
            payload = resp.json()
        except ValueError as e:
            last_error = f"Invalid JSON on attempt {attempt}/{max_retries}: {e}"
            logger.warning(last_error)
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
            continue

        if not payload:
            last_error = f"Empty JSON payload on attempt {attempt}/{max_retries}"
            logger.warning(last_error)
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
            continue

        return payload, None

    logger.error("fetch_json_with_retry exhausted all retries for %s: %s", url, last_error)
    return None, last_error


# ══════════════════════════════════════════════════════════════════════════
# 4. DATA FETCH + PARSE LAYER
# ══════════════════════════════════════════════════════════════════════════

def normalize_stock_symbol(raw: str) -> str:
    s = (raw or "").strip().upper()
    if s.endswith("-EQ"):
        s = s[:-3]
    if ":" in s:
        s = s.split(":")[-1]
    return s


@st.cache_data(ttl=15, show_spinner=False)
def fetch_option_chain_raw(symbol: str, is_index: bool) -> dict:
    """Cached (15s TTL) raw NSE option-chain JSON fetch. Returns a dict
    that always has the keys 'ok', 'payload', 'error' so callers never
    need to guess the shape of a failure. Cached at the Streamlit level
    so rapid re-renders (widget interactions) don't re-hit NSE."""
    session = get_nse_session()
    url = NSE_INDEX_CHAIN_URL if is_index else NSE_EQUITY_CHAIN_URL
    payload, error = fetch_json_with_retry(session, url, params={"symbol": symbol})
    if payload is None:
        return {"ok": False, "payload": None, "error": error or "No data returned."}
    records = payload.get("records") if isinstance(payload, dict) else None
    if not isinstance(records, dict) or not records.get("data"):
        return {"ok": False, "payload": payload, "error": "Response had no option-chain records."}
    return {"ok": True, "payload": payload, "error": None}


def _safe_num(val: Any, default: float = 0.0) -> float:
    try:
        if val is None:
            return default
        f = float(val)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except (TypeError, ValueError):
        return default


def parse_option_chain(payload: dict, preferred_expiry: str = "") -> tuple[pd.DataFrame, dict]:
    """Parses NSE's raw option-chain payload into a flat, numeric,
    NaN-free DataFrame plus a metadata dict (spot price, expiry list,
    selected expiry, fetch timestamp). Never raises: malformed rows are
    skipped individually rather than aborting the whole parse."""
    meta = {
        "spot_price": 0.0, "expiry_dates": [], "selected_expiry": "",
        "fetched_at": datetime.now(), "total_rows_seen": 0, "rows_parsed": 0,
    }
    records = payload.get("records", {}) if isinstance(payload, dict) else {}
    chain = records.get("data", []) if isinstance(records, dict) else []
    meta["spot_price"] = _safe_num(records.get("underlyingValue"))
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
        ce, pe = item.get("CE") or {}, item.get("PE") or {}
        rows.append({
            "strike_price": _safe_num(strike),
            "ce_ltp": _safe_num(ce.get("lastPrice")),
            "ce_change": _safe_num(ce.get("change")),
            "ce_bid": _safe_num(ce.get("bidprice")),
            "ce_bid_qty": _safe_num(ce.get("bidQty")),
            "ce_ask": _safe_num(ce.get("askPrice")),
            "ce_ask_qty": _safe_num(ce.get("askQty")),
            "ce_volume": _safe_num(ce.get("totalTradedVolume")),
            "ce_oi": _safe_num(ce.get("openInterest")),
            "ce_chng_oi": _safe_num(ce.get("changeinOpenInterest")),
            "ce_oi_change_pct": _safe_num(ce.get("pchangeinOpenInterest")),
            "ce_iv": _safe_num(ce.get("impliedVolatility")),
            "pe_ltp": _safe_num(pe.get("lastPrice")),
            "pe_change": _safe_num(pe.get("change")),
            "pe_bid": _safe_num(pe.get("bidprice")),
            "pe_bid_qty": _safe_num(pe.get("bidQty")),
            "pe_ask": _safe_num(pe.get("askPrice")),
            "pe_ask_qty": _safe_num(pe.get("askQty")),
            "pe_volume": _safe_num(pe.get("totalTradedVolume")),
            "pe_oi": _safe_num(pe.get("openInterest")),
            "pe_chng_oi": _safe_num(pe.get("changeinOpenInterest")),
            "pe_oi_change_pct": _safe_num(pe.get("pchangeinOpenInterest")),
            "pe_iv": _safe_num(pe.get("impliedVolatility")),
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
    try:
        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            return False
        if not all(c in df.columns for c in REQUIRED_CHAIN_COLUMNS):
            return False
        strikes = pd.to_numeric(df["strike_price"], errors="coerce").dropna()
        return bool((strikes > 0).sum() > 0)
    except Exception as e:  # noqa: BLE001 - validation must never raise
        logger.error("validate_chain_df raised an exception: %s", e)
        return False


def filter_strikes_around_atm(df: pd.DataFrame, spot: float, n_each_side: int) -> pd.DataFrame:
    if df is None or df.empty or n_each_side <= 0:
        return df
    d = df.sort_values("strike_price").reset_index(drop=True)
    ref = spot if spot else float(d["strike_price"].median())
    atm_idx = int((d["strike_price"] - ref).abs().idxmin())
    lo = max(0, atm_idx - n_each_side)
    hi = min(len(d), atm_idx + n_each_side + 1)
    return d.iloc[lo:hi].reset_index(drop=True)


def parse_days_to_expiry(expiry_label: str) -> float:
    """Returns days-to-expiry, floored at TRADING_DAYS_MIN_T so Black-
    Scholes never divides by (or takes log against) T=0 on expiry day."""
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


# ══════════════════════════════════════════════════════════════════════════
# 5. GREEKS ENGINE  (Black-Scholes: Delta / Gamma / Theta / Vega)
# ══════════════════════════════════════════════════════════════════════════

def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def bs_greeks(spot: float, strike: float, t_years: float, r: float, sigma: float,
              is_call: bool) -> dict[str, float]:
    """Standard Black-Scholes Greeks. sigma is annualized volatility as a
    fraction (0.18, not 18). Returns zeros (not NaN/inf) on any degenerate
    input so downstream DataFrame math never has to special-case this."""
    if spot <= 0 or strike <= 0 or t_years <= 0 or sigma <= 0:
        return {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    sigma = min(max(sigma, MIN_SIGMA), MAX_SIGMA)
    sqrt_t = math.sqrt(t_years)
    try:
        d1 = (math.log(spot / strike) + (r + 0.5 * sigma ** 2) * t_years) / (sigma * sqrt_t)
        d2 = d1 - sigma * sqrt_t
    except (ValueError, ZeroDivisionError):
        return {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}

    pdf_d1 = _norm_pdf(d1)
    gamma = pdf_d1 / (spot * sigma * sqrt_t)
    vega = spot * pdf_d1 * sqrt_t / 100.0  # per 1% (i.e. 0.01) change in vol

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
        "delta": round(delta, 4), "gamma": round(gamma, 6),
        "theta": round(theta, 4), "vega": round(vega, 4),
    }


def add_greeks_columns(df: pd.DataFrame, spot: float, expiry_label: str,
                        r: float = RISK_FREE_RATE) -> pd.DataFrame:
    """Adds ce_delta/ce_gamma/ce_theta/ce_vega and pe_* equivalents,
    computed from each strike's own NSE-supplied IV. Strikes with IV<=0
    (illiquid / no trades) get all-zero Greeks rather than a fabricated
    fallback volatility, since a fabricated IV would silently mislead
    the AI engine and GEX/DEX calculations that consume these columns."""
    d = df.copy()
    if d.empty:
        for col in ("ce_delta", "ce_gamma", "ce_theta", "ce_vega",
                    "pe_delta", "pe_gamma", "pe_theta", "pe_vega"):
            d[col] = 0.0
        return d

    t_years = parse_days_to_expiry(expiry_label) / 365.0

    ce_g = d.apply(
        lambda row: bs_greeks(spot, row["strike_price"], t_years, r, row["ce_iv"] / 100.0, True),
        axis=1,
    )
    pe_g = d.apply(
        lambda row: bs_greeks(spot, row["strike_price"], t_years, r, row["pe_iv"] / 100.0, False),
        axis=1,
    )
    for key in ("delta", "gamma", "theta", "vega"):
        d[f"ce_{key}"] = ce_g.apply(lambda x: x[key])
        d[f"pe_{key}"] = pe_g.apply(lambda x: x[key])
    return d


# ══════════════════════════════════════════════════════════════════════════
# 6. IV RANK / IV PERCENTILE  (session-based rolling history)
# ══════════════════════════════════════════════════════════════════════════

IV_HISTORY_KEY = "oc_atm_iv_history"
IV_HISTORY_MAX_POINTS = 500


def _atm_iv(df: pd.DataFrame, spot: float) -> float:
    if df.empty or not spot:
        return 0.0
    idx = (df["strike_price"] - spot).abs().idxmin()
    row = df.loc[idx]
    ivs = [v for v in (row.get("ce_iv", 0), row.get("pe_iv", 0)) if v and v > 0]
    return float(np.mean(ivs)) if ivs else 0.0


def update_iv_history(symbol: str, expiry_label: str, atm_iv: float) -> None:
    """Appends this refresh's ATM IV to a session-scoped rolling history,
    keyed per symbol+expiry so switching instruments doesn't pollute
    another instrument's IV Rank/Percentile calculation. This is a
    within-session history (resets when the Streamlit process restarts) —
    a genuine multi-day IV Rank needs a persisted historical IV series,
    which this standalone script does not have a database for."""
    if atm_iv <= 0:
        return
    history = st.session_state.setdefault(IV_HISTORY_KEY, {})
    key = f"{symbol}|{expiry_label}"
    series = history.get(key, [])
    series.append(atm_iv)
    if len(series) > IV_HISTORY_MAX_POINTS:
        series = series[-IV_HISTORY_MAX_POINTS:]
    history[key] = series
    st.session_state[IV_HISTORY_KEY] = history


def compute_iv_rank_percentile(symbol: str, expiry_label: str, current_iv: float) -> tuple[float, float]:
    """IV Rank = where current IV sits between this session's observed
    min/max (0-100). IV Percentile = % of session observations at or
    below current IV. Both return 0.0 until enough history has
    accumulated (first refresh) rather than a misleading fabricated 50."""
    history = st.session_state.get(IV_HISTORY_KEY, {})
    series = history.get(f"{symbol}|{expiry_label}", [])
    if len(series) < 2 or current_iv <= 0:
        return 0.0, 0.0
    lo, hi = min(series), max(series)
    iv_rank = ((current_iv - lo) / (hi - lo)) * 100 if hi > lo else 50.0
    iv_percentile = (sum(1 for v in series if v <= current_iv) / len(series)) * 100
    return round(float(np.clip(iv_rank, 0, 100)), 1), round(iv_percentile, 1)


# ══════════════════════════════════════════════════════════════════════════
# 7. GAMMA EXPOSURE (GEX) / DELTA EXPOSURE (DEX)
# ══════════════════════════════════════════════════════════════════════════

def compute_gex_dex(df: pd.DataFrame, spot: float, lot_size: int) -> dict[str, Any]:
    """Dealer-perspective Gamma/Delta Exposure approximation, computed
    per strike and summed. Convention used (standard retail approximation):
    dealers are assumed net SHORT calls and net SHORT puts they've sold
    to buyers, so:
        GEX_strike = (ce_gamma * ce_oi - pe_gamma * pe_oi) * spot^2 * 0.01 * lot_size
        DEX_strike = (ce_delta * ce_oi + pe_delta * pe_oi) * spot * lot_size
    Positive total GEX implies dealers hedge by buying dips/selling rips
    (dampening volatility); negative GEX implies the opposite (amplifying
    moves). This is a heuristic widely used in retail options analytics,
    not a certified market-maker positioning feed — no such feed exists
    publicly for NSE."""
    if df.empty or not spot:
        return {"total_gex": 0.0, "total_dex": 0.0, "by_strike": pd.DataFrame(),
                "max_gex_strike": None, "min_gex_strike": None, "gamma_flip": None}

    d = df.copy()
    d["gex"] = (
        (d.get("ce_gamma", 0) * d.get("ce_oi", 0)) - (d.get("pe_gamma", 0) * d.get("pe_oi", 0))
    ) * (spot ** 2) * 0.01 * lot_size
    d["dex"] = (
        (d.get("ce_delta", 0) * d.get("ce_oi", 0)) + (d.get("pe_delta", 0) * d.get("pe_oi", 0))
    ) * spot * lot_size

    total_gex = float(d["gex"].sum())
    total_dex = float(d["dex"].sum())
    max_gex_row = d.loc[d["gex"].idxmax()] if len(d) else None
    min_gex_row = d.loc[d["gex"].idxmin()] if len(d) else None

    # Gamma flip: the strike nearest to where cumulative GEX (sorted by
    # strike) crosses from negative to positive — an approximate proxy
    # for the "gamma flip point" some options-flow tools reference.
    d_sorted = d.sort_values("strike_price").reset_index(drop=True)
    cum_gex = d_sorted["gex"].cumsum()
    gamma_flip = None
    sign_changes = np.where(np.diff(np.sign(cum_gex.replace(0, np.nan).ffill().fillna(0))) != 0)[0]
    if len(sign_changes) > 0:
        idx = int(sign_changes[0])
        gamma_flip = float(d_sorted.loc[idx, "strike_price"])

    return {
        "total_gex": total_gex, "total_dex": total_dex,
        "by_strike": d[["strike_price", "gex", "dex"]],
        "max_gex_strike": float(max_gex_row["strike_price"]) if max_gex_row is not None else None,
        "min_gex_strike": float(min_gex_row["strike_price"]) if min_gex_row is not None else None,
        "gamma_flip": gamma_flip,
    }


# ══════════════════════════════════════════════════════════════════════════
# 8. CORE ANALYTICS — PCR / MAX PAIN / SUPPORT-RESISTANCE / BUILDUP / MONEYNESS
# ══════════════════════════════════════════════════════════════════════════

def calc_pcr(df: pd.DataFrame) -> float:
    if df.empty:
        return 0.0
    total_ce = df["ce_oi"].sum()
    total_pe = df["pe_oi"].sum()
    return round(float(total_pe / total_ce), 3) if total_ce > 0 else 0.0


def calc_max_pain(df: pd.DataFrame) -> float:
    if df.empty:
        return 0.0
    strikes = df["strike_price"].values
    ce_oi = df["ce_oi"].values
    pe_oi = df["pe_oi"].values
    pain = [
        float(np.sum(np.maximum(s - strikes, 0) * ce_oi) + np.sum(np.maximum(strikes - s, 0) * pe_oi))
        for s in strikes
    ]
    return float(strikes[int(np.argmin(pain))]) if pain else 0.0


def calc_max_oi(df: pd.DataFrame) -> dict[str, Optional[float]]:
    if df.empty:
        return {"max_ce_oi_strike": None, "max_pe_oi_strike": None}
    return {
        "max_ce_oi_strike": float(df.loc[df["ce_oi"].idxmax(), "strike_price"]),
        "max_pe_oi_strike": float(df.loc[df["pe_oi"].idxmax(), "strike_price"]),
    }


def calc_support_resistance(df: pd.DataFrame) -> tuple[Optional[float], Optional[float]]:
    """Support = strike with the highest Put OI (put writers defend this
    level). Resistance = strike with the highest Call OI (call writers
    defend this level). Standard options-chain heuristic."""
    if df.empty:
        return None, None
    support = float(df.loc[df["pe_oi"].idxmax(), "strike_price"])
    resistance = float(df.loc[df["ce_oi"].idxmax(), "strike_price"])
    return support, resistance


def classify_buildup(df: pd.DataFrame) -> pd.DataFrame:
    """Classifies each strike's CE and PE independently into Long
    Buildup / Short Buildup / Long Unwinding / Short Covering / Flat,
    using the standard price-vs-OI-change matrix (applied to that
    option's own LTP change, not the underlying's):
        Price Up   + OI Up   -> Long Buildup   (bullish for that option)
        Price Up   + OI Down -> Short Covering (bullish for that option)
        Price Down + OI Up   -> Short Buildup  (bearish for that option)
        Price Down + OI Down -> Long Unwinding (bearish for that option)
    Also derives Call Writing / Put Writing / Call Unwinding / Put
    Unwinding directly from OI-change sign, which is the simpler and
    more commonly quoted version of the same signal.
    """
    d = df.copy()

    def _matrix(price_chg: float, oi_chg: float) -> str:
        if price_chg > 0 and oi_chg > 0:
            return "Long Buildup"
        if price_chg > 0 and oi_chg < 0:
            return "Short Covering"
        if price_chg < 0 and oi_chg > 0:
            return "Short Buildup"
        if price_chg < 0 and oi_chg < 0:
            return "Long Unwinding"
        return "Flat"

    d["CE Buildup"] = d.apply(lambda r: _matrix(r.get("ce_change", 0), r.get("ce_chng_oi", 0)), axis=1)
    d["PE Buildup"] = d.apply(lambda r: _matrix(r.get("pe_change", 0), r.get("pe_chng_oi", 0)), axis=1)

    d["Call Writing"] = d["ce_chng_oi"] > 0
    d["Call Unwinding"] = d["ce_chng_oi"] < 0
    d["Put Writing"] = d["pe_chng_oi"] > 0
    d["Put Unwinding"] = d["pe_chng_oi"] < 0
    return d


def classify_moneyness(df: pd.DataFrame, spot: float) -> pd.DataFrame:
    """Tags each strike's CE and PE as ITM / ATM / OTM relative to spot.
    ATM = the single strike nearest spot; everything else is a strict
    ITM/OTM classification (a call is ITM below spot, a put is ITM
    above spot)."""
    d = df.copy()
    if d.empty:
        d["ATM"] = False
        d["CE Moneyness"] = ""
        d["PE Moneyness"] = ""
        return d
    ref = spot if spot else float(d["strike_price"].median())
    atm_idx = (d["strike_price"] - ref).abs().idxmin()
    d["ATM"] = d.index == atm_idx
    d["CE Moneyness"] = np.where(
        d["ATM"], "ATM", np.where(d["strike_price"] < ref, "ITM", "OTM")
    )
    d["PE Moneyness"] = np.where(
        d["ATM"], "ATM", np.where(d["strike_price"] > ref, "ITM", "OTM")
    )
    return d


OI_SHIFT_HISTORY_KEY = "oc_prev_support_resistance"


def detect_oi_shift(symbol: str, expiry_label: str, support: Optional[float],
                     resistance: Optional[float]) -> list[str]:
    """Compares this refresh's Support/Resistance against the previous
    refresh stored in session state for the same symbol+expiry, and
    reports any level shift. First refresh for a symbol+expiry has
    nothing to compare against, so it legitimately reports no shift."""
    notes = []
    history = st.session_state.setdefault(OI_SHIFT_HISTORY_KEY, {})
    key = f"{symbol}|{expiry_label}"
    prev = history.get(key)
    if prev:
        if prev.get("support") is not None and support is not None and support != prev["support"]:
            direction = "up" if support > prev["support"] else "down"
            notes.append(f"Support shifted {direction}: {prev['support']:,.0f} -> {support:,.0f}")
        if prev.get("resistance") is not None and resistance is not None and resistance != prev["resistance"]:
            direction = "up" if resistance > prev["resistance"] else "down"
            notes.append(f"Resistance shifted {direction}: {prev['resistance']:,.0f} -> {resistance:,.0f}")
    history[key] = {"support": support, "resistance": resistance}
    st.session_state[OI_SHIFT_HISTORY_KEY] = history
    return notes


def _normalize_series(series: pd.Series) -> pd.Series:
    s = series.astype(float)
    if s.empty:
        return s
    if s.max() == s.min():
        return pd.Series(0.5, index=s.index)
    return (s - s.min()) / (s.max() - s.min())


# ══════════════════════════════════════════════════════════════════════════
# 9. AI SIGNAL ENGINE — BUY/SELL/HOLD, INSTITUTIONAL, SMART MONEY
# ══════════════════════════════════════════════════════════════════════════

AI_SCORE_WEIGHTS = {
    "put_writing": 0.18, "call_unwind": 0.12, "volume": 0.12, "pcr_bias": 0.12,
    "proximity": 0.12, "max_pain_proximity": 0.10, "highest_oi": 0.10,
    "delta_oi_magnitude": 0.08, "iv_stability": 0.06,
}


def compute_ai_scores(df: pd.DataFrame, spot: float, atm_strike: float,
                       max_pain: float, pcr: float) -> pd.DataFrame:
    """Independent 0-100 CE Score / PE Score per strike, built from a
    weighted blend of OI buildup direction, volume, PCR bias, proximity
    to spot/max-pain, and IV stability. Mirrors the same signal families
    a discretionary options trader would read off a chain by eye."""
    d = df.copy()
    if d.empty:
        d["CE Score"] = pd.Series(dtype=float)
        d["PE Score"] = pd.Series(dtype=float)
        return d

    ce_oi_s = _normalize_series(d["ce_oi"])
    pe_oi_s = _normalize_series(d["pe_oi"])
    pe_chng_s = _normalize_series(d["pe_chng_oi"])
    ce_chng_s = _normalize_series(d["ce_chng_oi"])
    ce_unwind_s = _normalize_series((-d["ce_chng_oi"]).clip(lower=0))
    pe_unwind_s = _normalize_series((-d["pe_chng_oi"]).clip(lower=0))
    ce_vol_s = _normalize_series(d["ce_volume"])
    pe_vol_s = _normalize_series(d["pe_volume"])
    delta_oi_mag_s = _normalize_series(d["ce_chng_oi"].abs() + d["pe_chng_oi"].abs())

    avg_ce_iv = d.loc[d["ce_iv"] > 0, "ce_iv"].mean() if (d["ce_iv"] > 0).any() else 0.0
    avg_pe_iv = d.loc[d["pe_iv"] > 0, "pe_iv"].mean() if (d["pe_iv"] > 0).any() else 0.0
    ce_iv_stability_s = _normalize_series(-(d["ce_iv"] - avg_ce_iv).abs())
    pe_iv_stability_s = _normalize_series(-(d["pe_iv"] - avg_pe_iv).abs())

    ref = spot if spot else (atm_strike if atm_strike else float(d["strike_price"].median()))
    proximity_s = 1 - _normalize_series((d["strike_price"] - ref).abs())
    maxpain_proximity_s = 1 - _normalize_series((d["strike_price"] - max_pain).abs()) if max_pain else pd.Series(0.5, index=d.index)

    pcr_bull_bias = float(np.clip(((pcr or 1.0) - 1.0), -1, 1))
    pcr_bull_s = (pcr_bull_bias + 1) / 2
    pcr_bear_s = 1 - pcr_bull_s

    w = AI_SCORE_WEIGHTS
    ce_score = (
        pe_chng_s * w["put_writing"] + ce_unwind_s * w["call_unwind"] + ce_vol_s * w["volume"]
        + pcr_bull_s * w["pcr_bias"] + proximity_s * w["proximity"]
        + maxpain_proximity_s * w["max_pain_proximity"] + ce_oi_s * w["highest_oi"]
        + delta_oi_mag_s * w["delta_oi_magnitude"] + ce_iv_stability_s * w["iv_stability"]
    ) * 100

    pe_score = (
        ce_chng_s * w["put_writing"] + pe_unwind_s * w["call_unwind"] + pe_vol_s * w["volume"]
        + pcr_bear_s * w["pcr_bias"] + proximity_s * w["proximity"]
        + maxpain_proximity_s * w["max_pain_proximity"] + pe_oi_s * w["highest_oi"]
        + delta_oi_mag_s * w["delta_oi_magnitude"] + pe_iv_stability_s * w["iv_stability"]
    ) * 100

    d["CE Score"] = ce_score.clip(0, 100).round(1)
    d["PE Score"] = pe_score.clip(0, 100).round(1)

    def _decision(row) -> str:
        ce, pe = row["CE Score"], row["PE Score"]
        if abs(ce - pe) < 3:
            return "HOLD"
        return "BUY CE" if ce > pe else "BUY PE"

    d["AI Signal"] = d.apply(_decision, axis=1)
    d["AI Confidence %"] = d[["CE Score", "PE Score"]].max(axis=1).round(1)
    return d


def detect_institutional_smart_money(df: pd.DataFrame) -> pd.DataFrame:
    """Flags strikes showing institutional-scale positioning: OI in the
    top quartile combined with meaningful same-direction OI change and
    above-median volume (i.e. size AND fresh conviction AND liquidity,
    not just a stale large open position)."""
    d = df.copy()
    if d.empty:
        d["Institutional Signal"] = pd.Series(dtype=object)
        d["Smart Money"] = pd.Series(dtype=bool)
        return d

    ce_oi_q75 = d["ce_oi"].quantile(0.75) if d["ce_oi"].max() > 0 else 0
    pe_oi_q75 = d["pe_oi"].quantile(0.75) if d["pe_oi"].max() > 0 else 0
    ce_vol_med = d["ce_volume"].median()
    pe_vol_med = d["pe_volume"].median()

    def _inst_signal(row) -> str:
        ce_inst = row["ce_oi"] >= ce_oi_q75 > 0 and row["ce_chng_oi"] > 0 and row["ce_volume"] >= ce_vol_med
        pe_inst = row["pe_oi"] >= pe_oi_q75 > 0 and row["pe_chng_oi"] > 0 and row["pe_volume"] >= pe_vol_med
        if ce_inst and pe_inst:
            return "Institutional Activity (Both Sides)"
        if ce_inst:
            return "Institutional Call Writing"
        if pe_inst:
            return "Institutional Put Writing"
        return "None"

    d["Institutional Signal"] = d.apply(_inst_signal, axis=1)
    d["Smart Money"] = d["Institutional Signal"] != "None"
    return d


# ══════════════════════════════════════════════════════════════════════════
# 10. INTRADAY AI — BREAKOUT / REVERSAL / TREND PROBABILITY, SCALP/SWING
# ══════════════════════════════════════════════════════════════════════════

MOMENTUM_HISTORY_KEY = "oc_momentum_history"
MOMENTUM_HISTORY_MAX_POINTS = 30


def compute_momentum_score(spot: float, max_pain: float, pcr: float) -> float:
    mp_component = ((spot - max_pain) / max_pain) * 100 if (max_pain and spot) else 0.0
    return float(np.clip(((pcr - 1) * 50) + (mp_component * 0.5), -100, 100))


def update_momentum_history(symbol: str, expiry_label: str, momentum_score: float) -> None:
    history = st.session_state.setdefault(MOMENTUM_HISTORY_KEY, {})
    key = f"{symbol}|{expiry_label}"
    series = history.get(key, [])
    series.append(momentum_score)
    if len(series) > MOMENTUM_HISTORY_MAX_POINTS:
        series = series[-MOMENTUM_HISTORY_MAX_POINTS:]
    history[key] = series
    st.session_state[MOMENTUM_HISTORY_KEY] = history


def compute_trend_probability(symbol: str, expiry_label: str, momentum_score: float) -> float:
    """Trend probability derived from momentum consistency across this
    session's refreshes (a proxy for ADX-style trend strength) combined
    with the current momentum magnitude. Needs at least 3 refreshes of
    history to say anything about consistency; before that, it falls
    back to magnitude alone (scaled down to reflect low confidence)."""
    history = st.session_state.get(MOMENTUM_HISTORY_KEY, {})
    series = history.get(f"{symbol}|{expiry_label}", [])
    magnitude_component = min(abs(momentum_score), 100) / 100
    if len(series) < 3:
        return round(magnitude_component * 50, 1)
    same_sign = sum(1 for v in series[-5:] if np.sign(v) == np.sign(momentum_score) and v != 0)
    consistency_component = same_sign / min(len(series), 5)
    return round(float(np.clip((magnitude_component * 0.5 + consistency_component * 0.5) * 100, 0, 100)), 1)


def compute_breakout_reversal_probability(df: pd.DataFrame, spot: float, resistance: Optional[float],
                                           support: Optional[float], iv_rank: float) -> dict[str, float]:
    """Breakout probability rises when Call OI just above spot is thin
    relative to the chain (resistance weakening) and IV Rank is elevated
    (room for expansion). Reversal probability rises when the nearest
    OI wall (support/resistance) is unusually heavy relative to the
    chain (price is more likely to react at the wall than punch through)."""
    if df.empty or not spot:
        return {"breakout_probability": 0.0, "reversal_probability": 0.0}

    total_oi = (df["ce_oi"] + df["pe_oi"]).sum()
    avg_oi = total_oi / (2 * len(df)) if len(df) else 0

    resistance_oi = float(df.loc[df["strike_price"] == resistance, "ce_oi"].sum()) if resistance else 0.0
    support_oi = float(df.loc[df["strike_price"] == support, "pe_oi"].sum()) if support else 0.0
    nearest_wall_oi = max(resistance_oi, support_oi)

    wall_thinness = 1 - min(nearest_wall_oi / (avg_oi * 4), 1.0) if avg_oi > 0 else 0.5
    wall_heaviness = min(nearest_wall_oi / (avg_oi * 4), 1.0) if avg_oi > 0 else 0.5

    breakout_prob = float(np.clip((wall_thinness * 0.6 + (iv_rank / 100) * 0.4) * 100, 0, 100))
    reversal_prob = float(np.clip((wall_heaviness * 0.7 + (1 - iv_rank / 100) * 0.3) * 100, 0, 100))
    return {"breakout_probability": round(breakout_prob, 1), "reversal_probability": round(reversal_prob, 1)}


def compute_scalping_swing_signal(pcr: float, momentum_score: float, trend_probability: float,
                                   gex_dex: dict, iv_rank: float) -> dict[str, str]:
    """Scalping/Swing signals derived purely from this refresh's option-
    chain positioning (PCR, momentum, GEX/DEX, IV Rank) since this
    standalone script has no broker connection and therefore no live
    candle/tick feed to base a price-action scalp call on. Explicitly
    labelled as an OI-positioning read, not a price-action signal —
    always confirm with a live chart before acting."""
    total_gex = gex_dex.get("total_gex", 0.0)
    total_dex = gex_dex.get("total_dex", 0.0)

    scalp_score = (
        (1 if momentum_score > 15 else (-1 if momentum_score < -15 else 0))
        + (1 if total_dex > 0 else (-1 if total_dex < 0 else 0))
        + (1 if pcr > 1.1 else (-1 if pcr < 0.9 else 0))
    )
    if scalp_score >= 2:
        scalp_signal = "SCALP BUY CE"
    elif scalp_score <= -2:
        scalp_signal = "SCALP BUY PE"
    else:
        scalp_signal = "WAIT"

    swing_score = (
        (1 if trend_probability > 60 and momentum_score > 0 else (-1 if trend_probability > 60 and momentum_score < 0 else 0))
        + (1 if total_gex < 0 else 0)  # negative GEX -> moves tend to extend, favors swing continuation
        + (1 if iv_rank < 40 else (-1 if iv_rank > 75 else 0))  # cheap IV favors buying premium for a swing
    )
    if swing_score >= 2:
        swing_signal = "SWING BUY CE"
    elif swing_score <= -2:
        swing_signal = "SWING BUY PE"
    else:
        swing_signal = "WAIT"

    return {"scalping_signal": scalp_signal, "swing_signal": swing_signal}


# ══════════════════════════════════════════════════════════════════════════
# 11. CHARTS  (Plotly)
# ══════════════════════════════════════════════════════════════════════════

def _plotly_dark_layout(fig: go.Figure, height: int = 420, title: str = "") -> go.Figure:
    fig.update_layout(
        paper_bgcolor=DARK_BG, plot_bgcolor=DARK_BG,
        font=dict(color=TEXT_MUTED, family="Courier New"),
        height=height, margin=dict(l=10, r=10, t=40 if title else 10, b=10),
        title=dict(text=title, font=dict(color=TEXT_MAIN, size=14)) if title else None,
        legend=dict(bgcolor=PANEL_BG, bordercolor=BORDER_COLOR, borderwidth=1),
    )
    return fig


def chart_oi_bars(df: pd.DataFrame, max_pain: float) -> go.Figure:
    fig = make_subplots(rows=1, cols=2, subplot_titles=("Call OI (CE)", "Put OI (PE)"),
                         shared_yaxes=True, horizontal_spacing=0.04)
    if df.empty:
        return _plotly_dark_layout(fig)
    max_oi = max(df["ce_oi"].max(), df["pe_oi"].max(), 1)
    strikes_sorted = df["strike_price"].sort_values().unique()
    gap = (strikes_sorted[1] - strikes_sorted[0]) if len(strikes_sorted) > 1 else 1

    fig.add_trace(go.Bar(
        x=-df["ce_oi"], y=df["strike_price"], orientation="h",
        marker_color=[GREEN if abs(s - max_pain) < gap / 2 else "#238636" for s in df["strike_price"]],
        name="CE OI", showlegend=False,
        hovertemplate="Strike %{y}<br>CE OI: %{customdata:,}<extra></extra>", customdata=df["ce_oi"],
    ), row=1, col=1)
    fig.add_trace(go.Bar(
        x=df["pe_oi"], y=df["strike_price"], orientation="h",
        marker_color=[RED if abs(s - max_pain) < gap / 2 else "#da3633" for s in df["strike_price"]],
        name="PE OI", showlegend=False,
        hovertemplate="Strike %{y}<br>PE OI: %{x:,}<extra></extra>",
    ), row=1, col=2)
    for col in (1, 2):
        fig.add_hline(y=max_pain, line_dash="dot", line_color=AMBER,
                      annotation_text=f"Max Pain {max_pain:,.0f}", annotation_font_color=AMBER, row=1, col=col)
    fig.update_layout(
        xaxis=dict(showticklabels=False, showgrid=False, range=[-max_oi * 1.1, 0]),
        xaxis2=dict(showticklabels=False, showgrid=False, range=[0, max_oi * 1.1]),
        yaxis=dict(showgrid=True, gridcolor=BORDER_COLOR, tickfont=dict(color=TEXT_MAIN, size=11)),
    )
    fig.update_annotations(font_color=TEXT_MUTED)
    return _plotly_dark_layout(fig, height=480)


def chart_iv_skew(df: pd.DataFrame) -> go.Figure:
    fig = go.Figure()
    if not df.empty:
        fig.add_trace(go.Scatter(x=df["strike_price"], y=df["ce_iv"], mode="lines+markers",
                                  name="CE IV", line=dict(color=GREEN, width=2)))
        fig.add_trace(go.Scatter(x=df["strike_price"], y=df["pe_iv"], mode="lines+markers",
                                  name="PE IV", line=dict(color=RED, width=2)))
    fig.update_layout(xaxis=dict(title="Strike", showgrid=True, gridcolor=BORDER_COLOR),
                       yaxis=dict(title="IV %", showgrid=True, gridcolor=BORDER_COLOR))
    return _plotly_dark_layout(fig, height=320, title="Implied Volatility Skew")


def chart_greeks(df: pd.DataFrame, greek: str) -> go.Figure:
    fig = go.Figure()
    col_ce, col_pe = f"ce_{greek}", f"pe_{greek}"
    if not df.empty and col_ce in df.columns:
        fig.add_trace(go.Scatter(x=df["strike_price"], y=df[col_ce], mode="lines+markers",
                                  name=f"CE {greek.title()}", line=dict(color=GREEN, width=2)))
        fig.add_trace(go.Scatter(x=df["strike_price"], y=df[col_pe], mode="lines+markers",
                                  name=f"PE {greek.title()}", line=dict(color=RED, width=2)))
    fig.update_layout(xaxis=dict(title="Strike", showgrid=True, gridcolor=BORDER_COLOR),
                       yaxis=dict(title=greek.title(), showgrid=True, gridcolor=BORDER_COLOR))
    return _plotly_dark_layout(fig, height=300, title=f"{greek.title()} by Strike")


def chart_gex_by_strike(gex_data: dict) -> go.Figure:
    fig = go.Figure()
    by_strike = gex_data.get("by_strike", pd.DataFrame())
    if not by_strike.empty:
        colors = [GREEN if v >= 0 else RED for v in by_strike["gex"]]
        fig.add_trace(go.Bar(x=by_strike["strike_price"], y=by_strike["gex"], marker_color=colors, name="GEX"))
    fig.update_layout(xaxis=dict(title="Strike", showgrid=True, gridcolor=BORDER_COLOR),
                       yaxis=dict(title="Gamma Exposure", showgrid=True, gridcolor=BORDER_COLOR))
    return _plotly_dark_layout(fig, height=320, title="Gamma Exposure (GEX) by Strike")


def gauge_pcr(pcr: float) -> go.Figure:
    fig = go.Figure(go.Indicator(
        mode="gauge+number", value=pcr,
        number={"font": {"color": TEXT_MAIN, "size": 32, "family": "Courier New"}},
        gauge={
            "axis": {"range": [0, 3], "tickcolor": TEXT_MUTED, "tickfont": {"color": TEXT_MUTED}},
            "bar": {"color": BLUE, "thickness": 0.25}, "bgcolor": PANEL_BG, "borderwidth": 0,
            "steps": [{"range": [0, 0.7], "color": "#3b0d1a"}, {"range": [0.7, 1.3], "color": "#1c2128"},
                      {"range": [1.3, 3.0], "color": "#0d3b2e"}],
            "threshold": {"line": {"color": AMBER, "width": 3}, "value": pcr},
        },
        title={"text": "PUT / CALL RATIO", "font": {"color": TEXT_MUTED, "size": 12}},
    ))
    return _plotly_dark_layout(fig, height=220)


def gauge_momentum(score: float) -> go.Figure:
    fig = go.Figure(go.Indicator(
        mode="gauge+number", value=score,
        number={"font": {"color": TEXT_MAIN, "size": 30, "family": "Courier New"}},
        gauge={
            "axis": {"range": [-100, 100], "tickcolor": TEXT_MUTED, "tickfont": {"color": TEXT_MUTED}},
            "bar": {"color": BLUE, "thickness": 0.25}, "bgcolor": PANEL_BG, "borderwidth": 0,
            "steps": [{"range": [-100, -20], "color": "#3b0d1a"}, {"range": [-20, 20], "color": "#1c2128"},
                      {"range": [20, 100], "color": "#0d3b2e"}],
            "threshold": {"line": {"color": AMBER, "width": 3}, "value": score},
        },
        title={"text": "MOMENTUM SCORE", "font": {"color": TEXT_MUTED, "size": 12}},
    ))
    return _plotly_dark_layout(fig, height=220)


# ══════════════════════════════════════════════════════════════════════════
# 12. STYLED / HEATMAPPED CHAIN TABLE  (HTML render — full control over
#     color coding without relying on pandas Styler + Streamlit quirks)
# ══════════════════════════════════════════════════════════════════════════

_TABLE_CSS = f"""
<style>
.oc-table-wrap {{ max-height: 620px; overflow-y: auto; border: 1px solid {BORDER_COLOR}; border-radius: 8px; }}
.oc-table {{ width: 100%; border-collapse: collapse; font-family: 'Courier New', monospace; font-size: 12.5px; }}
.oc-table th {{ background: #1F4E78; color: #ffffff; padding: 8px 10px; text-align: center;
                position: sticky; top: 0; font-size: 11px; text-transform: uppercase; letter-spacing: .04em; }}
.oc-table td {{ padding: 6px 9px; text-align: center; border-bottom: 1px solid #21262d; color: {TEXT_MAIN}; white-space: nowrap; }}
.oc-atm-row td {{ background-color: #1c2128 !important; font-weight: 700; }}
</style>
"""


def _safe_cell(val: Any) -> str:
    """HTML-escapes a value before it is interpolated into a raw <td>,
    guarding against any stray '<'/'>'/'&' in a string field and against
    NaN rendering as the literal text 'nan'."""
    if val is None:
        return ""
    try:
        if isinstance(val, float) and math.isnan(val):
            return ""
    except (TypeError, ValueError):
        pass
    s = str(val)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _oi_cell_style(val: float, heavy_thresh: float, max_val: float) -> str:
    if max_val <= 0:
        return f"color:{TEXT_MUTED};"
    pct = max(0.0, min(100.0, (val / max_val) * 100))
    intensity = 0.10 + pct / 250
    is_heavy = heavy_thresh > 0 and val >= heavy_thresh
    bg = f"background:linear-gradient(90deg, rgba(63,185,80,{intensity:.2f}) {pct:.0f}%, transparent {pct:.0f}%);"
    weight = "font-weight:700;" if is_heavy else ""
    return bg + weight


def _oi_change_cell_style(val: float, heavy_thresh: float) -> str:
    if val == 0:
        return f"color:{TEXT_MUTED};"
    is_large = heavy_thresh > 0 and abs(val) >= heavy_thresh
    if val > 0:
        return f"color:#0d3b2e;font-weight:700;background-color:{GREEN};" if is_large else f"color:{GREEN};"
    return f"color:#3b0d1a;font-weight:700;background-color:{RED};" if is_large else f"color:{RED};"


def _signal_cell_style(val: str) -> str:
    v = str(val).upper()
    if "BUY CE" in v or "STRONG BUY" in v:
        return f"color:{GREEN};font-weight:700;"
    if "BUY PE" in v or "SELL" in v:
        return f"color:{RED};font-weight:700;"
    if "HOLD" in v or "WAIT" in v:
        return f"color:{AMBER};font-weight:700;"
    return f"color:{TEXT_MUTED};"


def render_chain_table_html(df: pd.DataFrame, show_greeks: bool, top_n: int = 400) -> str:
    if df.empty:
        return _TABLE_CSS + "<div style='color:#8b949e;padding:12px;'>No rows to display.</div>"

    base_cols = [
        ("ce_oi", "CE OI"), ("ce_chng_oi", "CE ΔOI"), ("ce_oi_change_pct", "CE ΔOI%"),
        ("ce_volume", "CE Vol"), ("ce_iv", "CE IV"), ("ce_ltp", "CE LTP"),
        ("ce_bid", "CE Bid"), ("ce_ask", "CE Ask"),
    ]
    greek_ce_cols = [("ce_delta", "CE Δ"), ("ce_gamma", "CE Γ"), ("ce_theta", "CE Θ"), ("ce_vega", "CE V")]
    mid_cols = [("strike_price", "STRIKE"), ("CE Buildup", "CE Build"), ("PE Buildup", "PE Build"),
                ("AI Signal", "AI Signal")]
    greek_pe_cols = [("pe_delta", "PE Δ"), ("pe_gamma", "PE Γ"), ("pe_theta", "PE Θ"), ("pe_vega", "PE V")]
    pe_cols = [
        ("pe_bid", "PE Bid"), ("pe_ask", "PE Ask"), ("pe_ltp", "PE LTP"), ("pe_iv", "PE IV"),
        ("pe_volume", "PE Vol"), ("pe_oi_change_pct", "PE ΔOI%"), ("pe_chng_oi", "PE ΔOI"), ("pe_oi", "PE OI"),
    ]

    cols = base_cols + (greek_ce_cols if show_greeks else []) + mid_cols + \
        (greek_pe_cols if show_greeks else []) + pe_cols
    cols = [(k, label) for k, label in cols if k in df.columns]

    fmt = {
        "ce_oi": "{:,.0f}", "ce_chng_oi": "{:+,.0f}", "ce_oi_change_pct": "{:+.1f}%",
        "ce_volume": "{:,.0f}", "ce_iv": "{:.1f}", "ce_ltp": "{:.2f}", "ce_bid": "{:.2f}", "ce_ask": "{:.2f}",
        "ce_delta": "{:.3f}", "ce_gamma": "{:.5f}", "ce_theta": "{:.3f}", "ce_vega": "{:.3f}",
        "strike_price": "{:,.0f}",
        "pe_delta": "{:.3f}", "pe_gamma": "{:.5f}", "pe_theta": "{:.3f}", "pe_vega": "{:.3f}",
        "pe_bid": "{:.2f}", "pe_ask": "{:.2f}", "pe_ltp": "{:.2f}", "pe_iv": "{:.1f}",
        "pe_volume": "{:,.0f}", "pe_oi_change_pct": "{:+.1f}%", "pe_chng_oi": "{:+,.0f}", "pe_oi": "{:,.0f}",
    }

    heavy_ce_oi = df["ce_oi"].quantile(0.80) if df["ce_oi"].max() > 0 else 0
    heavy_pe_oi = df["pe_oi"].quantile(0.80) if df["pe_oi"].max() > 0 else 0
    heavy_ce_chng = df["ce_chng_oi"].abs().quantile(0.80) if (df["ce_chng_oi"] != 0).any() else 0
    heavy_pe_chng = df["pe_chng_oi"].abs().quantile(0.80) if (df["pe_chng_oi"] != 0).any() else 0
    max_ce_oi, max_pe_oi = df["ce_oi"].max(), df["pe_oi"].max()

    view = df.head(top_n)
    header_html = "".join(f"<th>{label}</th>" for _, label in cols)
    rows_html = []
    for _, row in view.iterrows():
        is_atm = bool(row.get("ATM", False))
        cells = []
        for key, _ in cols:
            val = row.get(key, "")
            spec = fmt.get(key)
            display_val = spec.format(val) if spec and pd.notna(val) else ("" if pd.isna(val) else val)
            style = ""
            if key == "ce_oi":
                style = _oi_cell_style(val, heavy_ce_oi, max_ce_oi)
            elif key == "pe_oi":
                style = _oi_cell_style(val, heavy_pe_oi, max_pe_oi)
            elif key == "ce_chng_oi":
                style = _oi_change_cell_style(val, heavy_ce_chng)
            elif key == "pe_chng_oi":
                style = _oi_change_cell_style(val, heavy_pe_chng)
            elif key == "AI Signal":
                style = _signal_cell_style(val)
            cells.append(f'<td style="{style}">{_safe_cell(display_val)}</td>')
        row_class = "oc-atm-row" if is_atm else ""
        rows_html.append(f'<tr class="{row_class}">{"".join(cells)}</tr>')

    return (
        _TABLE_CSS
        + f'<div class="oc-table-wrap"><table class="oc-table"><thead><tr>{header_html}</tr></thead>'
        + f'<tbody>{"".join(rows_html)}</tbody></table></div>'
    )


# ══════════════════════════════════════════════════════════════════════════
# 13. REPORT EXPORT — EXCEL (openpyxl, conditional formatting) + CSV
# ══════════════════════════════════════════════════════════════════════════

FILL_HEADER = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
FILL_GREEN = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
FILL_RED = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
FILL_AMBER = PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid")
FONT_HEADER = Font(color="FFFFFF", bold=True, size=11)
THIN_BORDER = Border(*(Side(style="thin", color="30363D"),) * 4)


def _style_header_row(ws, row_idx: int = 1) -> None:
    for cell in ws[row_idx]:
        cell.fill = FILL_HEADER
        cell.font = FONT_HEADER
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = THIN_BORDER


def _autosize_columns(ws) -> None:
    for col_cells in ws.columns:
        length = max((len(str(c.value)) if c.value is not None else 0) for c in col_cells)
        col_letter = get_column_letter(col_cells[0].column)
        ws.column_dimensions[col_letter].width = min(max(length + 3, 10), 40)


def _apply_borders(ws) -> None:
    for row in ws.iter_rows():
        for cell in row:
            cell.border = THIN_BORDER


def _write_dataframe(ws, df: pd.DataFrame, start_row: int = 1) -> None:
    for j, col_name in enumerate(df.columns, start=1):
        ws.cell(row=start_row, column=j, value=str(col_name))
    for i, (_, row) in enumerate(df.iterrows(), start=start_row + 1):
        for j, val in enumerate(row, start=1):
            if isinstance(val, (np.integer,)):
                val = int(val)
            elif isinstance(val, (np.floating,)):
                val = float(val) if not math.isnan(val) else None
            elif isinstance(val, (np.bool_,)):
                val = bool(val)
            ws.cell(row=i, column=j, value=val)
    _style_header_row(ws, start_row)
    ws.freeze_panes = ws.cell(row=start_row + 1, column=1).coordinate
    ws.auto_filter.ref = ws.dimensions
    _conditional_color_signal_columns(ws, list(df.columns), start_row=start_row + 1)
    _apply_borders(ws)
    _autosize_columns(ws)


def _conditional_color_signal_columns(ws, header_values: list, start_row: int) -> None:
    target_cols = [
        idx + 1 for idx, h in enumerate(header_values)
        if h and any(k in str(h) for k in ("Signal", "Buildup", "Institutional", "Smart Money"))
    ]
    for row in ws.iter_rows(min_row=start_row):
        for col_idx in target_cols:
            cell = row[col_idx - 1]
            val = str(cell.value or "").upper()
            fill = None
            if "BUY CE" in val or "LONG BUILDUP" in val or "INSTITUTIONAL" in val or "TRUE" in val:
                fill = FILL_GREEN
            elif "BUY PE" in val or "SHORT BUILDUP" in val:
                fill = FILL_RED
            elif "HOLD" in val or "WAIT" in val or "FLAT" in val:
                fill = FILL_AMBER
            if fill:
                cell.fill = fill


def export_excel_report(df: pd.DataFrame, meta: dict, pcr: float, max_pain: float,
                         support: Optional[float], resistance: Optional[float],
                         symbol: str, expiry_label: str, iv_rank: float,
                         iv_percentile: float, gex_dex: dict) -> io.BytesIO:
    """Builds a multi-sheet Excel report: Summary, Option Chain, AI Signals,
    Greeks — fully formatted (colored headers, conditional fills, auto
    column width, freeze panes, borders, auto-filter)."""
    wb = Workbook()

    ws_summary = wb.active
    ws_summary.title = "Summary"
    summary_rows = [
        ("Symbol", symbol), ("Expiry", expiry_label),
        ("Generated At", datetime.now().strftime("%d-%b-%Y %H:%M:%S")),
        ("Spot Price", round(meta.get("spot_price", 0.0), 2)),
        ("PCR", pcr), ("Max Pain", max_pain),
        ("Support (Max PE OI)", support), ("Resistance (Max CE OI)", resistance),
        ("IV Rank", iv_rank), ("IV Percentile", iv_percentile),
        ("Total GEX", round(gex_dex.get("total_gex", 0.0), 2)),
        ("Total DEX", round(gex_dex.get("total_dex", 0.0), 2)),
        ("Gamma Flip Strike", gex_dex.get("gamma_flip")),
        ("Total CE OI", int(df["ce_oi"].sum()) if not df.empty else 0),
        ("Total PE OI", int(df["pe_oi"].sum()) if not df.empty else 0),
    ]
    ws_summary.cell(row=1, column=1, value="Metric")
    ws_summary.cell(row=1, column=2, value="Value")
    _style_header_row(ws_summary, 1)
    for i, (label, value) in enumerate(summary_rows, start=2):
        ws_summary.cell(row=i, column=1, value=label)
        ws_summary.cell(row=i, column=2, value=value)
    ws_summary.freeze_panes = "A2"
    _apply_borders(ws_summary)
    _autosize_columns(ws_summary)

    ws_chain = wb.create_sheet("Option Chain")
    chain_export_cols = [c for c in [
        "strike_price", "ce_oi", "ce_chng_oi", "ce_oi_change_pct", "ce_volume", "ce_iv", "ce_ltp",
        "ce_bid", "ce_ask", "CE Buildup", "CE Moneyness", "AI Signal", "AI Confidence %",
        "Institutional Signal", "Smart Money", "PE Moneyness", "PE Buildup",
        "pe_bid", "pe_ask", "pe_ltp", "pe_iv", "pe_volume", "pe_oi_change_pct", "pe_chng_oi", "pe_oi",
    ] if c in df.columns]
    _write_dataframe(ws_chain, df[chain_export_cols])

    ws_greeks = wb.create_sheet("Greeks")
    greek_cols = [c for c in [
        "strike_price", "ce_delta", "ce_gamma", "ce_theta", "ce_vega",
        "pe_delta", "pe_gamma", "pe_theta", "pe_vega",
    ] if c in df.columns]
    if greek_cols:
        _write_dataframe(ws_greeks, df[greek_cols])

    ws_signals = wb.create_sheet("AI Signals")
    signal_cols = [c for c in [
        "strike_price", "AI Signal", "AI Confidence %", "CE Score", "PE Score",
        "Institutional Signal", "Smart Money",
    ] if c in df.columns]
    if signal_cols:
        sig_df = df[signal_cols].sort_values("AI Confidence %", ascending=False) if "AI Confidence %" in df.columns else df[signal_cols]
        _write_dataframe(ws_signals, sig_df)

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer


def export_csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8")


# ══════════════════════════════════════════════════════════════════════════
# 14. STREAMLIT UI — PAGE CONFIG, CSS, SUMMARY CARDS
# ══════════════════════════════════════════════════════════════════════════

def _configure_page() -> None:
    """Guarded: set_page_config() must be Streamlit's first command and
    can only run once per session. Caught and logged rather than raising,
    so importing this module from another app.py doesn't crash it."""
    try:
        st.set_page_config(
            page_title="NSE Options Chain Dashboard", page_icon="📊",
            layout="wide", initial_sidebar_state="expanded",
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("st.set_page_config() skipped (not first Streamlit command): %s", e)


def _inject_css() -> None:
    st.markdown(f"""
    <style>
    .stApp {{ background-color: {DARK_BG}; }}
    section[data-testid="stSidebar"] {{ background-color: {PANEL_BG}; border-right: 1px solid {BORDER_COLOR}; }}
    div[data-testid="metric-container"] {{
        background: {PANEL_BG}; border: 1px solid {BORDER_COLOR}; border-radius: 8px; padding: 14px 18px;
    }}
    div[data-testid="metric-container"] label {{ color: {TEXT_MUTED} !important; font-size: 12px;
        text-transform: uppercase; letter-spacing: 0.08em; }}
    div[data-testid="metric-container"] div[data-testid="stMetricValue"] {{
        color: {TEXT_MAIN} !important; font-size: 21px; font-weight: 700; font-family: 'Courier New', monospace; }}
    h1, h2, h3 {{ color: {TEXT_MAIN} !important; }}
    .block-title {{ color: {BLUE}; font-size: 13px; font-weight: 600; text-transform: uppercase;
        letter-spacing: 0.1em; margin-bottom: 8px; }}
    button[data-baseweb="tab"] {{ color: {TEXT_MUTED} !important; }}
    button[data-baseweb="tab"][aria-selected="true"] {{ color: {BLUE} !important; border-bottom: 2px solid {BLUE}; }}
    hr {{ border-color: {BORDER_COLOR}; }}
    .intel-card {{ background: {PANEL_BG}; border: 1px solid {BORDER_COLOR}; border-radius: 8px;
        padding: 14px 16px; margin-bottom: 8px; }}
    .intel-label {{ color: {TEXT_MUTED}; font-size: 11px; text-transform: uppercase; letter-spacing: .08em; }}
    .intel-value {{ color: {TEXT_MAIN}; font-size: 20px; font-weight: 700; font-family: 'Courier New', monospace; }}
    </style>
    """, unsafe_allow_html=True)


def _pcr_sentiment_badge(pcr: float) -> str:
    if pcr > 1.3:
        return f'<span style="color:{GREEN};font-weight:700;">🟢 Bullish (High PCR)</span>'
    if pcr < 0.7:
        return f'<span style="color:{RED};font-weight:700;">🔴 Bearish (Low PCR)</span>'
    return f'<span style="color:{AMBER};font-weight:700;">🟡 Neutral</span>'


# ══════════════════════════════════════════════════════════════════════════
# 15. MAIN DASHBOARD
# ══════════════════════════════════════════════════════════════════════════

def _sidebar_config() -> dict:
    with st.sidebar:
        st.markdown("### ⚙️ Configuration")
        instrument_type = st.radio("Instrument Type", ["Index", "F&O Stock"], key="oc_instr_type")
        is_index = instrument_type == "Index"

        if is_index:
            symbol = st.selectbox("Index", list(INDEX_SYMBOLS.keys()), key="oc_index_select")
        else:
            raw_symbol = st.text_input(
                "Stock Symbol (e.g. RELIANCE, TCS, INFY, SBIN, HDFCBANK)", "RELIANCE", key="oc_stock_input"
            )
            symbol = normalize_stock_symbol(raw_symbol)

        strike_count = st.slider("Strikes Around ATM", 5, 40, 15, step=5, key="oc_strike_count")
        show_greeks = st.checkbox("Show Greeks columns in chain table", value=True, key="oc_show_greeks")
        min_ai_conf = st.slider("Min AI Confidence % (signals list)", 0, 100, 55, step=5, key="oc_min_ai_conf")
        strike_search = st.number_input(
            "Search / highlight a specific strike (0 = off)", min_value=0, value=0, step=50, key="oc_strike_search"
        )

        default_lot = DEFAULT_LOT_SIZES.get(symbol, DEFAULT_LOT_SIZES["_STOCK_DEFAULT"])
        lot_size = st.number_input(
            "Lot Size (used for GEX/DEX — verify against current NSE circular)",
            min_value=1, value=default_lot, step=1, key="oc_lot_size",
        )

        st.divider()
        st.markdown("### 🔄 Auto Refresh")
        auto_refresh = st.checkbox("Enable auto-refresh", value=False, key="oc_auto_refresh")
        refresh_secs = st.slider("Refresh interval (seconds)", 10, 120, 20, step=5, key="oc_refresh_secs",
                                  disabled=not auto_refresh)

        st.divider()
        debug_mode = st.checkbox("Show raw API debug info", value=False, key="oc_debug_mode")
        fetch_clicked = st.button("🔄 Fetch Live Data", use_container_width=True, type="primary")

    return {
        "is_index": is_index, "symbol": symbol, "strike_count": strike_count,
        "show_greeks": show_greeks, "min_ai_conf": min_ai_conf, "strike_search": strike_search,
        "lot_size": lot_size, "auto_refresh": auto_refresh, "refresh_secs": refresh_secs,
        "debug_mode": debug_mode, "fetch_clicked": fetch_clicked,
    }


def _do_fetch_and_process(cfg: dict) -> Optional[dict]:
    """Runs the full fetch -> parse -> validate -> analytics pipeline.
    Returns None (after showing an st.error) on unrecoverable failure so
    the caller can bail out cleanly; otherwise returns a dict bundling
    every computed artifact the UI needs."""
    raw_result = fetch_option_chain_raw(cfg["symbol"], cfg["is_index"])
    if cfg["debug_mode"]:
        st.write("**Fetch result (ok/error):**", raw_result.get("ok"), raw_result.get("error"))

    if not raw_result.get("ok"):
        st.error(
            f"⚠️ Could not fetch the NSE option chain for **{cfg['symbol']}**: "
            f"{raw_result.get('error') or 'Unknown error.'} "
            "This can happen if NSE is rate-limiting, the market is closed and the endpoint is "
            "temporarily unavailable, or the symbol has no listed options. Try again in a moment."
        )
        return None

    payload = raw_result["payload"]
    preferred_expiry = st.session_state.get("oc_selected_expiry", "")
    df_all, meta = parse_option_chain(payload, preferred_expiry=preferred_expiry)

    if not validate_chain_df(df_all):
        st.error(
            f"⚠️ NSE returned a response for **{cfg['symbol']}**, but it did not contain a usable "
            "option chain (missing strikes/LTP/OI). This can happen right after market open or for "
            "an illiquid stock with no active option series. Please try again shortly."
        )
        return None

    spot = meta["spot_price"]
    df = filter_strikes_around_atm(df_all, spot, cfg["strike_count"])
    if df.empty:
        df = df_all

    expiry_label = meta["selected_expiry"]
    atm_strike = float(df.iloc[(df["strike_price"] - spot).abs().argsort().iloc[0]]["strike_price"]) if spot else \
        float(df["strike_price"].median())

    df = add_greeks_columns(df, spot, expiry_label)
    df = classify_buildup(df)
    df = classify_moneyness(df, spot)
    df = compute_ai_scores(df, spot, atm_strike, calc_max_pain(df), calc_pcr(df))
    df = detect_institutional_smart_money(df)

    pcr = calc_pcr(df)
    max_pain = calc_max_pain(df)
    support, resistance = calc_support_resistance(df)
    max_oi = calc_max_oi(df)

    atm_iv = _atm_iv(df, spot)
    update_iv_history(cfg["symbol"], expiry_label, atm_iv)
    iv_rank, iv_percentile = compute_iv_rank_percentile(cfg["symbol"], expiry_label, atm_iv)

    gex_dex = compute_gex_dex(df, spot, cfg["lot_size"])

    momentum_score = compute_momentum_score(spot, max_pain, pcr)
    update_momentum_history(cfg["symbol"], expiry_label, momentum_score)
    trend_probability = compute_trend_probability(cfg["symbol"], expiry_label, momentum_score)
    breakout_reversal = compute_breakout_reversal_probability(df, spot, resistance, support, iv_rank)
    scalp_swing = compute_scalping_swing_signal(pcr, momentum_score, trend_probability, gex_dex, iv_rank)
    oi_shift_notes = detect_oi_shift(cfg["symbol"], expiry_label, support, resistance)

    return {
        "df": df, "meta": meta, "spot": spot, "atm_strike": atm_strike, "expiry_label": expiry_label,
        "pcr": pcr, "max_pain": max_pain, "support": support, "resistance": resistance, "max_oi": max_oi,
        "atm_iv": atm_iv, "iv_rank": iv_rank, "iv_percentile": iv_percentile, "gex_dex": gex_dex,
        "momentum_score": momentum_score, "trend_probability": trend_probability,
        "breakout_reversal": breakout_reversal, "scalp_swing": scalp_swing,
        "oi_shift_notes": oi_shift_notes,
    }


def _render_summary_cards(state: dict) -> None:
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Spot Price", f"₹{state['spot']:,.2f}" if state["spot"] else "—")
    c2.metric("ATM Strike", f"₹{state['atm_strike']:,.0f}")
    c3.metric("PCR", f"{state['pcr']:.3f}")
    c4.metric("Max Pain", f"₹{state['max_pain']:,.0f}")
    c5.metric("IV Rank / %ile", f"{state['iv_rank']:.0f} / {state['iv_percentile']:.0f}")

    c6, c7, c8, c9, c10 = st.columns(5)
    c6.metric("Support (Max PE OI)", f"₹{state['support']:,.0f}" if state["support"] else "—")
    c7.metric("Resistance (Max CE OI)", f"₹{state['resistance']:,.0f}" if state["resistance"] else "—")
    c8.metric("Total GEX", f"{state['gex_dex'].get('total_gex', 0):,.0f}")
    c9.metric("Total DEX", f"{state['gex_dex'].get('total_dex', 0):,.0f}")
    c10.metric("Momentum Score", f"{state['momentum_score']:+.1f}")


def _render_ai_signal_cards(state: dict, min_conf: float) -> None:
    df = state["df"]
    qualifying = df[df["AI Confidence %"] >= min_conf].sort_values("AI Confidence %", ascending=False)
    if qualifying.empty:
        st.info(
            f"No strikes currently meet the {min_conf:.0f}% AI confidence threshold. "
            "Lower the threshold in the sidebar or wait for the next refresh."
        )
        return
    for _, row in qualifying.head(15).iterrows():
        signal = row["AI Signal"]
        color = GREEN if "CE" in signal else (RED if "PE" in signal else AMBER)
        st.markdown(f"""
        <div class="intel-card">
          <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;">
            <div><b style="color:{TEXT_MAIN};">{row['strike_price']:,.0f}</b>
              &nbsp; <span style="color:{color};font-weight:700;">{_safe_cell(signal)}</span></div>
            <div class="intel-label">Confidence
              <span style="color:{TEXT_MAIN};font-weight:700;font-size:15px;">{row['AI Confidence %']:.0f}%</span></div>
          </div>
          <div style="margin-top:8px;color:{TEXT_MUTED};font-size:12px;">
            CE Score {row['CE Score']:.1f} &nbsp;|&nbsp; PE Score {row['PE Score']:.1f}
            &nbsp;|&nbsp; {_safe_cell(row.get('Institutional Signal', 'None'))}
            &nbsp;|&nbsp; CE {_safe_cell(row.get('CE Buildup', ''))} / PE {_safe_cell(row.get('PE Buildup', ''))}
          </div>
        </div>
        """, unsafe_allow_html=True)


def run_dashboard() -> None:
    _configure_page()
    _inject_css()
    st.markdown("## 📊 NSE Options Chain Dashboard — AI Engine")

    cfg = _sidebar_config()

    if cfg["symbol"] != st.session_state.get("oc_last_symbol"):
        st.session_state["oc_last_symbol"] = cfg["symbol"]
        st.session_state.pop("oc_state", None)
        st.session_state.pop("oc_selected_expiry", None)

    if cfg["fetch_clicked"] or cfg["auto_refresh"]:
        with st.spinner(f"Fetching live NSE option chain for {cfg['symbol']} …"):
            result = _do_fetch_and_process(cfg)
        if result is not None:
            st.session_state["oc_state"] = result
            st.session_state["oc_selected_expiry"] = result["expiry_label"]

    state = st.session_state.get("oc_state")
    if state is None:
        st.info("👈 Choose an instrument in the sidebar and click **Fetch Live Data** to begin.")
        return

    df: pd.DataFrame = state["df"]
    meta = state["meta"]

    expiry_options = meta.get("expiry_dates", [])
    if expiry_options:
        current = state["expiry_label"] if state["expiry_label"] in expiry_options else expiry_options[0]
        selected = st.selectbox(
            "Expiry", expiry_options, index=expiry_options.index(current), key="oc_expiry_selectbox"
        )
        if selected != st.session_state.get("oc_selected_expiry"):
            st.session_state["oc_selected_expiry"] = selected
            with st.spinner("Reloading chain for selected expiry …"):
                refreshed = _do_fetch_and_process(cfg)
            if refreshed is not None:
                st.session_state["oc_state"] = refreshed
                state = refreshed
                df = state["df"]

    if cfg["debug_mode"]:
        with st.expander("🔍 Debug info", expanded=False):
            st.write("Rows seen / parsed:", meta.get("total_rows_seen"), "/", meta.get("rows_parsed"))
            st.write("Expiry dates from API:", expiry_options)
            st.dataframe(df.head(5), use_container_width=True)

    _render_summary_cards(state)
    st.markdown(f"📡 Sentiment: {_pcr_sentiment_badge(state['pcr'])}", unsafe_allow_html=True)

    for note in state.get("oi_shift_notes", []):
        st.info(f"🔀 OI Shift — {note}")

    if cfg["strike_search"]:
        match = df[df["strike_price"] == cfg["strike_search"]]
        if not match.empty:
            r = match.iloc[0]
            st.success(
                f"🔎 Strike {cfg['strike_search']:,.0f} — CE LTP {r['ce_ltp']:.2f} (OI {r['ce_oi']:,.0f}) | "
                f"PE LTP {r['pe_ltp']:.2f} (OI {r['pe_oi']:,.0f}) | AI Signal: {r['AI Signal']}"
            )
        else:
            st.warning(f"Strike {cfg['strike_search']:,.0f} is not in the currently loaded strike range.")

    st.divider()

    tab_chain, tab_charts, tab_greeks, tab_ai, tab_gex, tab_export = st.tabs([
        "📋 Option Chain", "📈 Charts", "🧮 Greeks", "🤖 AI Signals",
        "⚡ GEX / DEX", "📥 Export",
    ])

    with tab_chain:
        st.markdown(render_chain_table_html(df, cfg["show_greeks"]), unsafe_allow_html=True)
        st.caption(
            "CE/PE OI cells are heat-shaded relative to the heaviest OI strike in this view. "
            "ΔOI cells are green (OI rising) or red (OI falling), with a solid fill marking the "
            "top-20% largest moves. Buildup labels use each option's own price-change vs OI-change "
            "matrix (Long/Short Buildup, Long Unwinding, Short Covering)."
        )

    with tab_charts:
        st.plotly_chart(chart_oi_bars(df, state["max_pain"]), use_container_width=True,
                         config={"displayModeBar": False})
        col_a, col_b = st.columns(2)
        with col_a:
            st.plotly_chart(gauge_pcr(state["pcr"]), use_container_width=True, config={"displayModeBar": False})
        with col_b:
            st.plotly_chart(gauge_momentum(state["momentum_score"]), use_container_width=True,
                             config={"displayModeBar": False})
        st.plotly_chart(chart_iv_skew(df), use_container_width=True, config={"displayModeBar": False})

    with tab_greeks:
        g1, g2 = st.columns(2)
        with g1:
            st.plotly_chart(chart_greeks(df, "delta"), use_container_width=True, config={"displayModeBar": False})
            st.plotly_chart(chart_greeks(df, "theta"), use_container_width=True, config={"displayModeBar": False})
        with g2:
            st.plotly_chart(chart_greeks(df, "gamma"), use_container_width=True, config={"displayModeBar": False})
            st.plotly_chart(chart_greeks(df, "vega"), use_container_width=True, config={"displayModeBar": False})
        st.caption(
            "Greeks are Black-Scholes values computed from each strike's own NSE-supplied IV, "
            f"time-to-expiry from '{state['expiry_label']}', and a {RISK_FREE_RATE*100:.0f}% risk-free "
            "rate. Strikes with no traded IV (illiquid) show zero Greeks rather than an assumed value."
        )

    with tab_ai:
        st.markdown('<div class="block-title">🤖 AI Trade Signals</div>', unsafe_allow_html=True)
        _render_ai_signal_cards(state, cfg["min_ai_conf"])

        st.markdown("<br>", unsafe_allow_html=True)
        st.markdown('<div class="block-title">📈 Intraday Probabilities</div>', unsafe_allow_html=True)
        p1, p2, p3 = st.columns(3)
        p1.metric("Breakout Probability", f"{state['breakout_reversal']['breakout_probability']:.0f}%")
        p2.metric("Reversal Probability", f"{state['breakout_reversal']['reversal_probability']:.0f}%")
        p3.metric("Trend Probability", f"{state['trend_probability']:.0f}%")

        p4, p5 = st.columns(2)
        p4.metric("Scalping Signal", state["scalp_swing"]["scalping_signal"])
        p5.metric("Swing Signal", state["scalp_swing"]["swing_signal"])
        st.caption(
            "Scalping/Swing signals are derived purely from this refresh's option-chain positioning "
            "(PCR, momentum, GEX/DEX, IV Rank) — this standalone script has no broker/candle feed, so "
            "these are OI-positioning reads, not price-action signals. Always confirm with a live "
            "chart before acting. This is not financial advice."
        )

    with tab_gex:
        e1, e2, e3 = st.columns(3)
        e1.metric("Total Gamma Exposure", f"{state['gex_dex'].get('total_gex', 0):,.0f}")
        e2.metric("Total Delta Exposure", f"{state['gex_dex'].get('total_dex', 0):,.0f}")
        gf = state["gex_dex"].get("gamma_flip")
        e3.metric("Gamma Flip Strike (approx.)", f"{gf:,.0f}" if gf else "—")
        st.plotly_chart(chart_gex_by_strike(state["gex_dex"]), use_container_width=True,
                         config={"displayModeBar": False})
        st.caption(
            "GEX/DEX use the standard retail dealer-short approximation: "
            "GEX = (CE Gamma·CE OI − PE Gamma·PE OI)·Spot²·0.01·LotSize, "
            "DEX = (CE Delta·CE OI + PE Delta·PE OI)·Spot·LotSize. "
            "Verify the lot size in the sidebar against the current NSE circular before relying on "
            "the absolute magnitude — the sign and relative shape are the more robust read."
        )

    with tab_export:
        st.markdown('<div class="block-title">📥 Export Reports</div>', unsafe_allow_html=True)
        col_x, col_y = st.columns(2)
        with col_x:
            try:
                excel_buf = export_excel_report(
                    df, meta, state["pcr"], state["max_pain"], state["support"], state["resistance"],
                    cfg["symbol"], state["expiry_label"], state["iv_rank"], state["iv_percentile"],
                    state["gex_dex"],
                )
                st.download_button(
                    "⬇️ Download Excel Report", data=excel_buf,
                    file_name=f"option_chain_{cfg['symbol']}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    use_container_width=True,
                )
            except Exception as e:  # noqa: BLE001
                st.error(f"Could not build Excel report: {e}")
        with col_y:
            try:
                csv_bytes = export_csv_bytes(df)
                st.download_button(
                    "⬇️ Download CSV", data=csv_bytes,
                    file_name=f"option_chain_{cfg['symbol']}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                    mime="text/csv", use_container_width=True,
                )
            except Exception as e:  # noqa: BLE001
                st.error(f"Could not build CSV export: {e}")

    st.caption(
        f"Data source: NSE India public option-chain API · Last fetched: "
        f"{meta.get('fetched_at', datetime.now()).strftime('%H:%M:%S')} · "
        "Educational/analytical tool — not financial advice."
    )

    if cfg["auto_refresh"]:
        time.sleep(cfg["refresh_secs"])
        st.rerun()


# ══════════════════════════════════════════════════════════════════════════
# 16. ENTRY POINT / HOSTING-APP COMPATIBILITY SHIM
# ══════════════════════════════════════════════════════════════════════════

def show_option_chain(fyers: Any = None) -> None:
    """Compatibility entry point for hosting apps (e.g. app.py) that call
    `from option_chain import show_option_chain` and invoke it as
    `show_option_chain(fyers)`. This dashboard fetches directly from
    NSE's public option-chain API and does not require a broker
    connection, so `fyers` is accepted for signature compatibility but
    is intentionally unused. Passing None (or nothing) works exactly
    the same as passing an authenticated FYERS client — it is simply
    ignored — so no changes are required in app.py."""
    if fyers is not None:
        logger.info(
            "show_option_chain() received a `fyers` client, but this module "
            "fetches directly from NSE's public API and does not use a "
            "broker connection — the fyers argument is accepted only for "
            "backward compatibility with the hosting app's import signature."
        )
    run_dashboard()


if __name__ == "__main__":
    run_dashboard()
