import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import numpy as np
import math
from datetime import datetime

# ─── Page Config ────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Options Chain Dashboard",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─── Helpers ────────────────────────────────────────────────────────────────
def calculate_max_pain(df):
    strikes = df["strike_price"].values
    ce_oi, pe_oi = df["ce_oi"].values, df["pe_oi"].values
    pain = [np.sum(np.maximum(s - strikes, 0) * ce_oi) + np.sum(np.maximum(strikes - s, 0) * pe_oi) for s in strikes]
    return float(strikes[int(np.argmin(pain))])

def pcr_signal(pcr):
    if pcr > 1.3: return '<span class="signal-bull">🟢 Bullish (High PCR)</span>'
    elif pcr < 0.7: return '<span class="signal-bear">🔴 Bearish (Low PCR)</span>'
    return '<span class="signal-neu">🟡 Neutral</span>'

# ─── Fetching Logic ─────────────────────────────────────────────────────────
def fetch_optionchain_with_fallback(fyers, symbol, strikecount, is_stock, expiry_timestamp=""):
    """
    API ద్వారా ఆప్షన్స్ డేటాను తెస్తుంది. ఎక్స్‌పైరీ డేటా ఉంటేనే timestamp పంపుతుంది.
    """
    attempts = []
    tried_symbols = [symbol]
    
    # స్టాక్ అయితే కరెక్ట్ సింబల్ ఫార్మాట్ కోసం
    if is_stock:
        # నార్మలైజ్ సింబల్ ఫంక్షన్ ఇక్కడ ఉండాలి
        alt = f"NSE:{symbol.split(':')[-1]}-EQ"
        if alt not in tried_symbols:
            tried_symbols.append(alt)

    last_response = None
    for sym in tried_symbols:
        req = {"symbol": sym, "strikecount": int(strikecount)}
        
        # KEY FIX: ఎక్స్‌పైరీ డేటా ఉంటేనే timestamp పంపాలి
        if expiry_timestamp and expiry_timestamp.strip():
            req["timestamp"] = expiry_timestamp.strip()
            
        try:
            resp = fyers.optionchain(data=req)
        except Exception as e:
            attempts.append((sym, f"exception: {e}"))
            continue
            
        attempts.append((sym, resp.get("s") if isinstance(resp, dict) else "no response"))
        last_response = resp
        if isinstance(resp, dict) and resp.get("s") == "ok":
            return resp, sym, attempts

    return last_response, tried_symbols[-1], attempts
# ─── Custom CSS ─────────────────────────────────────────────────────────────
st.markdown("""
<style>
    .stApp { background-color: #0d1117; }
    section[data-testid="stSidebar"] { background-color: #161b22; border-right: 1px solid #30363d; }

    div[data-testid="metric-container"] {
        background: #161b22;
        border: 1px solid #30363d;
        border-radius: 8px;
        padding: 16px 20px;
    }
    div[data-testid="metric-container"] label { color: #8b949e !important; font-size: 12px; text-transform: uppercase; letter-spacing: 0.08em; }
    div[data-testid="metric-container"] div[data-testid="stMetricValue"] { color: #e6edf3 !important; font-size: 24px; font-weight: 700; font-family: 'Courier New', monospace; }

    h1, h2, h3 { color: #e6edf3 !important; }
    .block-title { color: #58a6ff; font-size: 13px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.1em; margin-bottom: 8px; }

    .ce-badge { background: #0d3b2e; color: #3fb950; border: 1px solid #238636; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 700; }
    .pe-badge { background: #3b0d1a; color: #f85149; border: 1px solid #da3633; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 700; }

    button[data-baseweb="tab"] { color: #8b949e !important; }
    button[data-baseweb="tab"][aria-selected="true"] { color: #58a6ff !important; border-bottom: 2px solid #58a6ff; }

    .stDataFrame { border: 1px solid #30363d; border-radius: 8px; overflow: hidden; }
    hr { border-color: #30363d; }

    .signal-bull { background: #0d3b2e; color: #3fb950; border: 1px solid #238636; padding: 4px 14px; border-radius: 20px; font-size: 13px; font-weight: 700; display: inline-block; }
    .signal-bear { background: #3b0d1a; color: #f85149; border: 1px solid #da3633; padding: 4px 14px; border-radius: 20px; font-size: 13px; font-weight: 700; display: inline-block; }
    .signal-neu  { background: #1c2128; color: #d29922; border: 1px solid #9e6a03; padding: 4px 14px; border-radius: 20px; font-size: 13px; font-weight: 700; display: inline-block; }
</style>
""", unsafe_allow_html=True)

# ─── Helpers ────────────────────────────────────────────────────────────────
def calculate_max_pain(df: pd.DataFrame) -> float:
    strikes = df["strike_price"].values
    ce_oi = df["ce_oi"].values
    pe_oi = df["pe_oi"].values
    pain = []
    for s in strikes:
        ce_loss = np.sum(np.maximum(s - strikes, 0) * ce_oi)
        pe_loss = np.sum(np.maximum(strikes - s, 0) * pe_oi)
        pain.append(ce_loss + pe_loss)
    return float(strikes[int(np.argmin(pain))])


def pcr_signal(pcr: float) -> str:
    if pcr > 1.3:
        return '<span class="signal-bull">🟢 Bullish (High PCR)</span>'
    elif pcr < 0.7:
        return '<span class="signal-bear">🔴 Bearish (Low PCR)</span>'
    else:
        return '<span class="signal-neu">🟡 Neutral</span>'


def oi_bar_chart(df: pd.DataFrame, max_pain: float) -> go.Figure:
    fig = make_subplots(
        rows=1, cols=2,
        subplot_titles=("Call OI  (CE)", "Put OI  (PE)"),
        shared_yaxes=True,
        horizontal_spacing=0.04,
    )
    max_oi = max(df["ce_oi"].max(), df["pe_oi"].max())
    strikes_sorted = df["strike_price"].sort_values().unique()
    strike_gap = (strikes_sorted[1] - strikes_sorted[0]) if len(strikes_sorted) > 1 else 1

    fig.add_trace(go.Bar(
        x=-df["ce_oi"], y=df["strike_price"], orientation="h",
        marker_color=[
            "#1a7f37" if abs(s - max_pain) < strike_gap / 2 else "#238636"
            for s in df["strike_price"]
        ],
        name="CE OI", showlegend=False,
        customdata=df["ce_oi"],
        hovertemplate="Strike %{y}<br>CE OI: %{customdata:,}<extra></extra>",
    ), row=1, col=1)

    fig.add_trace(go.Bar(
        x=df["pe_oi"], y=df["strike_price"], orientation="h",
        marker_color=[
            "#b91c1c" if abs(s - max_pain) < strike_gap / 2 else "#da3633"
            for s in df["strike_price"]
        ],
        name="PE OI", showlegend=False,
        hovertemplate="Strike %{y}<br>PE OI: %{x:,}<extra></extra>",
    ), row=1, col=2)

    for col in [1, 2]:
        fig.add_hline(y=max_pain, line_dash="dot", line_color="#f0c814",
                      annotation_text=f"Max Pain {max_pain:,.0f}",
                      annotation_font_color="#f0c814", row=1, col=col)

    fig.update_layout(
        paper_bgcolor="#0d1117", plot_bgcolor="#0d1117",
        font=dict(color="#8b949e", family="Courier New"),
        height=500, margin=dict(l=10, r=10, t=40, b=10),
        xaxis=dict(showticklabels=False, zeroline=False, showgrid=False, range=[-max_oi * 1.1, 0]),
        xaxis2=dict(showticklabels=False, zeroline=False, showgrid=False, range=[0, max_oi * 1.1]),
        yaxis=dict(showgrid=True, gridcolor="#21262d", tickfont=dict(color="#e6edf3", size=11)),
    )
    fig.update_annotations(font_color="#8b949e")
    return fig


def pcr_gauge(pcr: float) -> go.Figure:
    fig = go.Figure(go.Indicator(
        mode="gauge+number",
        value=pcr,
        number={"font": {"color": "#e6edf3", "size": 36, "family": "Courier New"}, "suffix": ""},
        gauge={
            "axis": {"range": [0, 3], "tickcolor": "#8b949e", "tickfont": {"color": "#8b949e"}},
            "bar": {"color": "#58a6ff", "thickness": 0.25},
            "bgcolor": "#161b22",
            "borderwidth": 0,
            "steps": [
                {"range": [0, 0.7],   "color": "#3b0d1a"},
                {"range": [0.7, 1.3], "color": "#1c2128"},
                {"range": [1.3, 3.0], "color": "#0d3b2e"},
            ],
            "threshold": {"line": {"color": "#f0c814", "width": 3}, "value": pcr},
        },
        title={"text": "PUT/CALL RATIO", "font": {"color": "#8b949e", "size": 12}},
        domain={"x": [0, 1], "y": [0, 1]},
    ))
    fig.update_layout(
        paper_bgcolor="#0d1117", font=dict(color="#8b949e"),
        height=220, margin=dict(l=20, r=20, t=30, b=0),
    )
    return fig


def iv_chart(df: pd.DataFrame) -> go.Figure:
    has_ce_iv = "ce_iv" in df.columns
    has_pe_iv = "pe_iv" in df.columns
    fig = go.Figure()
    if has_ce_iv:
        fig.add_trace(go.Scatter(
            x=df["strike_price"], y=df["ce_iv"],
            mode="lines+markers", name="CE IV",
            line=dict(color="#238636", width=2),
            marker=dict(size=5),
        ))
    if has_pe_iv:
        fig.add_trace(go.Scatter(
            x=df["strike_price"], y=df["pe_iv"],
            mode="lines+markers", name="PE IV",
            line=dict(color="#da3633", width=2),
            marker=dict(size=5),
        ))
    if not has_ce_iv and not has_pe_iv:
        fig.add_annotation(text="IV data not available from this API response",
                           xref="paper", yref="paper", x=0.5, y=0.5,
                           font=dict(color="#8b949e"), showarrow=False)
    fig.update_layout(
        paper_bgcolor="#0d1117", plot_bgcolor="#0d1117",
        font=dict(color="#8b949e", family="Courier New"),
        height=280, margin=dict(l=10, r=10, t=10, b=30),
        xaxis=dict(showgrid=True, gridcolor="#21262d", title="Strike"),
        yaxis=dict(showgrid=True, gridcolor="#21262d", title="IV %"),
        legend=dict(bgcolor="#161b22", bordercolor="#30363d", borderwidth=1),
    )
    return fig


def style_chain_table(df: pd.DataFrame) -> pd.DataFrame:
    cols = ["ce_oi", "ce_chng_oi", "ce_volume", "ce_ltp", "CE Bias", "strike_price",
            "PE Bias", "pe_ltp", "pe_volume", "pe_chng_oi", "pe_oi", "Strike Signal", "Big Move"]
    available = [c for c in cols if c in df.columns]
    out = df[available].copy()

    rename = {
        "ce_oi": "CE OI", "ce_chng_oi": "CE ΔOI", "ce_volume": "CE Vol",
        "ce_ltp": "CE LTP", "strike_price": "Strike ⚡",
        "pe_ltp": "PE LTP", "pe_volume": "PE Vol",
        "pe_chng_oi": "PE ΔOI", "pe_oi": "PE OI",
    }
    out.rename(columns={k: v for k, v in rename.items() if k in out.columns}, inplace=True)
    return out


def extract_options_data(response: dict):
    """
    Fyers' option chain response has been inconsistent across SDK/API
    versions: the array of strikes can show up under different keys.
    Try the known variants instead of assuming a single key name.
    """
    data = response.get("data", {})
    for key in ("options", "optionsChain", "optionschain", "data"):
        candidate = data.get(key)
        if isinstance(candidate, list) and len(candidate) > 0:
            return candidate, data
    # Some responses nest spot price at top-level "data" with options
    # directly as a list (no "options" wrapper at all).
    if isinstance(data, list) and len(data) > 0:
        return data, {}
    return [], data


def extract_spot_price(response: dict, data) -> float:
    """
    Fyers' spot/underlying price has shown up under different keys
    across SDK/API versions and response shapes. Try the known
    variants in order instead of assuming a single key name, which is
    what was causing Spot Price to always show as nil/—.
    """
    candidates = []

    if isinstance(data, dict):
        candidates.extend([
            data.get("ltp"), data.get("spot_price"), data.get("spotPrice"),
            data.get("underlyingValue"), data.get("underlying_value"),
            data.get("underlyingLtp"), data.get("underlying_ltp"),
        ])

    # Some Fyers responses nest the spot price at the top level of the
    # response, not inside "data" at all.
    candidates.extend([
        response.get("ltp"), response.get("spot_price"), response.get("spotPrice"),
        response.get("underlyingValue"), response.get("underlying_value"),
    ])

    for val in candidates:
        try:
            f = float(val)
            if f > 0:
                return f
        except (TypeError, ValueError):
            continue
    return 0.0


def normalize_chain_shape(options_data: list) -> pd.DataFrame:
    """
    Fyers' optionchain response can come in two shapes:

    1. WIDE: one row per strike, with prefixed columns already
       (ce_oi, pe_oi, ce_ltp, pe_ltp, ...). Older code assumed this.

    2. LONG: one row per option contract — i.e. a separate row for the
       CE and a separate row for the PE at each strike — with a field
       like "option_type" ("CE"/"PE") and shared columns such as "oi",
       "ltp", "volume", "chng_oi". This is what newer Fyers v3 responses
       typically return, and is the most common cause of every column
       showing 0 (the wide ce_/pe_ prefixed columns simply don't exist).

    This function detects which shape we got and always returns a WIDE
    DataFrame with strike_price, ce_*, pe_* columns.
    """
    raw = pd.DataFrame(options_data)

    # Already wide (has at least one ce_/pe_ prefixed column) -> use as-is
    if any(c.startswith("ce_") or c.startswith("pe_") for c in raw.columns):
        return raw

    # Long format: look for an option-type-like column
    type_col = next(
        (c for c in ("option_type", "optionType", "type", "instrument_type") if c in raw.columns),
        None,
    )
    if type_col is None:
        # Unknown shape — return raw as-is, downstream code will fill zeros
        return raw

    raw[type_col] = raw[type_col].astype(str).str.upper()

    field_map = {
        "oi": "oi", "open_interest": "oi",
        "ltp": "ltp", "last_price": "ltp",
        "volume": "volume", "vol": "volume",
        "chng_oi": "chng_oi", "change_oi": "chng_oi", "oi_change": "chng_oi",
        "iv": "iv", "implied_volatility": "iv",
    }
    raw_renamed = raw.rename(columns={k: v for k, v in field_map.items() if k in raw.columns})

    value_cols = [c for c in ("oi", "ltp", "volume", "chng_oi", "iv") if c in raw_renamed.columns]

    ce_df = raw_renamed[raw_renamed[type_col] == "CE"][["strike_price"] + value_cols].copy()
    pe_df = raw_renamed[raw_renamed[type_col] == "PE"][["strike_price"] + value_cols].copy()

    ce_df.rename(columns={c: f"ce_{c}" for c in value_cols}, inplace=True)
    pe_df.rename(columns={c: f"pe_{c}" for c in value_cols}, inplace=True)

    wide = pd.merge(ce_df, pe_df, on="strike_price", how="outer")
    return wide


def detect_big_moves(df: pd.DataFrame, top_n: int = 3) -> list:
    """
    Flags strikes with unusually large open-interest buildup (chng_oi),
    which is commonly read as smart-money positioning. Large +ve CE
    chng_oi at a strike above spot = call writing (resistance forming /
    bearish near that strike); large +ve PE chng_oi below spot = put
    writing (support forming / bullish near that strike). Large OI
    UNWINDING (negative chng_oi) on the side that previously dominated
    suggests an existing position is being closed, often preceding a
    breakout through that strike.
    Returns a list of dicts: {strike, side, direction, oi_change, note}.
    """
    alerts = []
    if "ce_chng_oi" not in df.columns or "pe_chng_oi" not in df.columns:
        return alerts

    ce_thresh = df["ce_chng_oi"].abs().quantile(0.85) if df["ce_chng_oi"].abs().max() > 0 else 0
    pe_thresh = df["pe_chng_oi"].abs().quantile(0.85) if df["pe_chng_oi"].abs().max() > 0 else 0

    top_ce = df.reindex(df["ce_chng_oi"].abs().sort_values(ascending=False).index).head(top_n)
    top_pe = df.reindex(df["pe_chng_oi"].abs().sort_values(ascending=False).index).head(top_n)

    for _, row in top_ce.iterrows():
        chg = row["ce_chng_oi"]
        if abs(chg) < ce_thresh or chg == 0:
            continue
        if chg > 0:
            alerts.append({
                "strike": row["strike_price"], "side": "CE", "direction": "SELL",
                "oi_change": chg,
                "note": f"Heavy CALL writing at {row['strike_price']:,.0f} — resistance building, "
                        f"bearish/range bias near this strike. Consider SELL CE / avoid buying CE here.",
            })
        else:
            alerts.append({
                "strike": row["strike_price"], "side": "CE", "direction": "BUY",
                "oi_change": chg,
                "note": f"CALL OI unwinding at {row['strike_price']:,.0f} — resistance weakening, "
                        f"possible breakout above. Consider BUY CE on confirmation.",
            })

    for _, row in top_pe.iterrows():
        chg = row["pe_chng_oi"]
        if abs(chg) < pe_thresh or chg == 0:
            continue
        if chg > 0:
            alerts.append({
                "strike": row["strike_price"], "side": "PE", "direction": "BUY",
                "oi_change": chg,
                "note": f"Heavy PUT writing at {row['strike_price']:,.0f} — support building, "
                        f"bullish bias near this strike. Consider BUY CE / SELL PE here.",
            })
        else:
            alerts.append({
                "strike": row["strike_price"], "side": "PE", "direction": "SELL",
                "oi_change": chg,
                "note": f"PUT OI unwinding at {row['strike_price']:,.0f} — support weakening, "
                        f"possible breakdown below. Consider BUY PE on confirmation.",
            })

    alerts.sort(key=lambda a: abs(a["oi_change"]), reverse=True)
    return alerts


def extract_expiry_list(response: dict) -> list:
    """
    Fyers' optionchain response typically includes an 'expiryData' array
    (each item has a human-readable date and a unix 'expiry' timestamp).
    Returns a list of (label, timestamp) tuples sorted by date, or []
    if the response doesn't carry expiry info (e.g. before any fetch).
    """
    data = response.get("data", {}) if isinstance(response, dict) else {}
    raw = data.get("expiryData") or data.get("expirydata") or []
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        label = item.get("date") or item.get("expiry_date") or str(item.get("expiry", ""))
        ts = item.get("expiry") or item.get("timestamp")
        if ts is not None:
            out.append((label, str(ts)))
    return out


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bs_price(spot, strike, t, r, sigma, is_call: bool) -> float:
    if t <= 0 or sigma <= 0 or spot <= 0 or strike <= 0:
        return max(0.0, (spot - strike) if is_call else (strike - spot))
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma ** 2) * t) / (sigma * math.sqrt(t))
    d2 = d1 - sigma * math.sqrt(t)
    if is_call:
        return spot * _norm_cdf(d1) - strike * math.exp(-r * t) * _norm_cdf(d2)
    return strike * math.exp(-r * t) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def implied_volatility(price, spot, strike, t, is_call: bool, r: float = 0.07) -> float:
    """
    Newton-Raphson IV solver. Fyers' optionchain endpoint commonly does
    NOT return ce_iv/pe_iv fields at all, which is why the IV Skew chart
    was showing empty — IV needs to be derived from the option premium
    via Black-Scholes instead of read directly from the API response.
    Returns IV as a percentage (e.g. 18.5), or 0.0 if it can't solve.
    """
    if price <= 0 or spot <= 0 or strike <= 0 or t <= 0:
        return 0.0
    sigma = 0.3
    for _ in range(50):
        model_price = _bs_price(spot, strike, t, r, sigma, is_call)
        d1 = (math.log(spot / strike) + (r + 0.5 * sigma ** 2) * t) / (sigma * math.sqrt(t))
        vega = spot * math.exp(-0.5 * d1 ** 2) / math.sqrt(2 * math.pi) * math.sqrt(t)
        diff = model_price - price
        if abs(diff) < 1e-4:
            break
        if vega < 1e-8:
            break
        sigma -= diff / vega
        sigma = max(0.001, min(sigma, 5.0))
    return round(sigma * 100, 2)


def add_iv_columns(df: pd.DataFrame, spot: float, expiry_label: str) -> pd.DataFrame:
    """Derives ce_iv/pe_iv via Black-Scholes when the API didn't supply them."""
    if "ce_iv" in df.columns and "pe_iv" in df.columns and df["ce_iv"].abs().sum() > 0:
        return df  # API already gave usable IVs

    days_to_expiry = parse_days_to_expiry(expiry_label)
    t = max(days_to_expiry, 0.5) / 365.0

    if not spot:
        return df

    df = df.copy()
    df["ce_iv"] = df.apply(
        lambda row: implied_volatility(row.get("ce_ltp", 0), spot, row["strike_price"], t, True), axis=1
    )
    df["pe_iv"] = df.apply(
        lambda row: implied_volatility(row.get("pe_ltp", 0), spot, row["strike_price"], t, False), axis=1
    )
    return df


def parse_days_to_expiry(expiry_label: str) -> float:
    """Parses a 'DD-MM-YYYY' style expiry label into days-from-today. Falls back to 7 days if unparseable."""
    if not expiry_label:
        return 7.0
    for fmt in ("%d-%m-%Y", "%d-%b-%Y", "%Y-%m-%d"):
        try:
            exp_date = datetime.strptime(expiry_label, fmt)
            delta = (exp_date - datetime.now()).total_seconds() / 86400
            return max(delta, 0.5)
        except ValueError:
            continue
    return 7.0


def normalize_symbol(stock: str, with_eq: bool = False) -> str:
    """Build an NSE symbol for the option chain endpoint.
    Fyers SDK versions disagree on whether the underlying for stock
    options needs the '-EQ' suffix, so callers can try both."""
    stock = stock.strip().upper()
    stock = stock[:-3] if stock.endswith("-EQ") else stock
    return f"NSE:{stock}-EQ" if with_eq else f"NSE:{stock}"


def fetch_optionchain_with_fallback(fyers, symbol: str, strikecount: int, is_stock: bool, expiry_timestamp: str = ""):
    """
    Calls fyers.optionchain, retrying with an alternate symbol format if the
    first attempt returns a non-'ok' status (commonly a 300/invalid-input
    error caused by symbol formatting differences between Fyers SDK/API
    versions). Returns (response, symbol_used, attempts_log).
    """
    attempts = []
    tried_symbols = [symbol]

    # For stocks, also try the -EQ variant as a fallback since some
    # Fyers API versions require it for the optionchain endpoint.
    if is_stock:
        alt = normalize_symbol(symbol.split(":")[-1], with_eq=not symbol.endswith("-EQ"))
        if alt not in tried_symbols:
            tried_symbols.append(alt)

    last_response = None
    for sym in tried_symbols:
        req = {"symbol": sym, "strikecount": int(strikecount)}
        if expiry_timestamp:
            req["timestamp"] = expiry_timestamp
        try:
            resp = fyers.optionchain(data=req)
        except Exception as e:
            attempts.append((sym, f"exception: {e}"))
            continue
        attempts.append((sym, resp.get("s") if isinstance(resp, dict) else "no response"))
        last_response = resp
        if isinstance(resp, dict) and resp.get("s") == "ok":
            return resp, sym, attempts

    return last_response, tried_symbols[-1], attempts


def compute_strike_bias(df: pd.DataFrame) -> pd.DataFrame:
    """
    Classic OI-buildup read per strike, using change-in-OI direction as a
    proxy for where fresh positioning is happening (price-change data isn't
    reliably available from the chain endpoint, so this uses the standard
    simplification: rising Call OI = resistance/sell-side pressure building,
    rising Put OI = support/buy-side pressure building).

    Adds:
      - "CE Bias": Sell Side / Unwinding / Flat, based on ce_chng_oi
      - "PE Bias": Buy Side / Unwinding / Flat, based on pe_chng_oi
      - "Strike Signal": combined per-strike read (Buy/Sell/Neutral)
      - "Big Move": flags strikes whose |chng_oi| is in the top 20% of the
        chain for either leg — i.e. unusually large fresh activity.
    """
    out = df.copy()
    ce_chng = out["ce_chng_oi"] if "ce_chng_oi" in out.columns else pd.Series(0, index=out.index)
    pe_chng = out["pe_chng_oi"] if "pe_chng_oi" in out.columns else pd.Series(0, index=out.index)

    def ce_label(v):
        if v > 0:
            return "🔴 Sell Side (Call Writing)"
        elif v < 0:
            return "🟢 Unwinding"
        return "⚪ Flat"

    def pe_label(v):
        if v > 0:
            return "🟢 Buy Side (Put Writing)"
        elif v < 0:
            return "🔴 Unwinding"
        return "⚪ Flat"

    out["CE Bias"] = ce_chng.apply(ce_label)
    out["PE Bias"] = pe_chng.apply(pe_label)

    # Combined per-strike signal: compare which side has the larger fresh
    # OI build to call an overall lean for that strike.
    def combined(row):
        ce_v, pe_v = row["_ce_chng"], row["_pe_chng"]
        if pe_v > 0 and pe_v >= max(ce_v, 0):
            return "🟢 BUY"
        if ce_v > 0 and ce_v >= max(pe_v, 0):
            return "🔴 SELL"
        return "🟡 NEUTRAL"

    out["_ce_chng"] = ce_chng
    out["_pe_chng"] = pe_chng
    out["Strike Signal"] = out.apply(combined, axis=1)

    # Big-move flag: top 20% of |chng_oi| across either leg in this chain.
    magnitudes = pd.concat([ce_chng.abs(), pe_chng.abs()])
    threshold = magnitudes.quantile(0.8) if len(magnitudes) > 0 and magnitudes.max() > 0 else float("inf")
    out["Big Move"] = ((ce_chng.abs() >= threshold) | (pe_chng.abs() >= threshold)).map(
        {True: "🚨 Big Move", False: ""}
    )

    out.drop(columns=["_ce_chng", "_pe_chng"], inplace=True)
    return out


# ─── Main Function ───────────────────────────────────────────────────────────
def show_option_chain(fyers):
    st.markdown("## 📊 Master Options Chain Dashboard")
    # ... (మిగతా కోడ్) ...

    with st.sidebar:
        st.markdown("### ⚙️ Configuration")
       option_type = st.radio("Instrument Type", ["Indices", "F&O Stocks"], key="instr_type_radio")
        is_stock = option_type == "F&O Stocks"
        
        if not is_stock:
            selected_key = st.selectbox("Index", list(symbol_map.keys()))
            symbol = symbol_map[selected_key]
        else:
            stock = st.text_input("Stock Symbol (e.g. RELIANCE)", "RELIANCE")
            symbol = normalize_symbol(stock)

        # --- ఇక్కడ పేస్ట్ చేయండి (సింబల్ సెలెక్ట్ చేసుకున్న తర్వాత) ---
        if "current_symbol" not in st.session_state:
            st.session_state["current_symbol"] = symbol

        if st.session_state["current_symbol"] != symbol:
            st.session_state["oc_expiry_data"] = None  # పాత డేటాను క్లియర్ చేస్తుంది
            st.session_state["current_symbol"] = symbol
            st.rerun() # యాప్‌ని రిఫ్రెష్ చేస్తుంది
        # -----------------------------------------------------

        # ... (మిగిలిన కోడ్ - strike_count, expiry dropdown, మొదలైనవి) ...

    # ── Sidebar ──────────────────────────────────────────────────────────────
    with st.sidebar:
        st.markdown("### ⚙️ Configuration")
        symbol_map = {
            "NIFTY 50":       "NSE:NIFTY50-INDEX",
            "NIFTY BANK":     "NSE:NIFTYBANK-INDEX",
            "FINNIFTY":       "NSE:FINNIFTY-INDEX",
            "MIDCAP NIFTY":   "NSE:MIDCPNIFTY-INDEX",
            "SENSEX":         "BSE:SENSEX-INDEX",
            "BANKEX":         "BSE:BANKEX-INDEX",
            "NIFTY NXT 50":   "NSE:NIFTYNEXT50-INDEX",
        }
        option_type = st.radio("Instrument Type", ["Indices", "F&O Stocks"])
        is_stock = option_type == "F&O Stocks"
        if not is_stock:
            selected_key = st.selectbox("Index", list(symbol_map.keys()))
            symbol = symbol_map[selected_key]
        else:
            stock = st.text_input("Stock Symbol (e.g. RELIANCE)", "RELIANCE")
            symbol = normalize_symbol(stock)

        # Stock option chains commonly reject strikecount values that are
        # fine for indices; keep the stock max tighter to avoid 300 errors.
        max_strikes = 20 if is_stock else 30
        strike_count = st.slider("Strikes Around ATM", 5, max_strikes, min(20, max_strikes), step=5)

        expiry_options = st.session_state.get("oc_expiry_list", [])
        if expiry_options:
            expiry_labels = [label for label, _ in expiry_options]
            selected_expiry_label = st.selectbox("Expiry", expiry_labels)
            expiry_timestamp = dict(expiry_options).get(selected_expiry_label, "")
        else:
            st.caption("Expiry list loads after first fetch (uses nearest expiry by default).")
            expiry_timestamp = ""
            selected_expiry_label = ""

        debug_mode = st.checkbox("Show raw API response (debug)", value=False)
        st.divider()
        fetch_btn = st.button("🔄 Fetch Live Data", use_container_width=True, type="primary")

    # ── Fetch & Process ──────────────────────────────────────────────────────
    if fetch_btn:
        with st.spinner("Connecting to Fyers API …"):
            response, used_symbol, attempts = fetch_optionchain_with_fallback(
                fyers, symbol, strike_count, is_stock, expiry_timestamp
            )

        if debug_mode:
            st.write("**Symbols tried:**", attempts)
            st.json(response)

        if not response:
            st.error("API call failed for all symbol variants tried. Check your Fyers connection/token.")
            return

        if response.get("s") != "ok":
            err_code = response.get("code", "—")
            err_msg = response.get("message", "No data returned")
            st.error(
                f"API Error (code {err_code}): {err_msg}\n\n"
                f"Tried: {', '.join(s for s, _ in attempts)}. "
                "If this is a stock, confirm it actually has active F&O "
                "contracts on NSE — not every stock has listed options."
            )
            return

        symbol = used_symbol

        # Capture the expiry list from this response so the sidebar dropdown
        # populates for the *next* fetch (Fyers only returns expiryData
        # alongside the chain itself, so we can't know it beforehand).
        new_expiry_list = extract_expiry_list(response)
        if new_expiry_list:
            st.session_state["oc_expiry_list"] = new_expiry_list

        options_data, data = extract_options_data(response)
        spot_price = extract_spot_price(response, data)

        # Some option chain payloads simply don't carry the underlying
        # LTP at all. Fall back to a direct quotes call so Spot Price
        # doesn't show as nil/— even when this happens.
        if not spot_price:
            try:
                quote_resp = fyers.quotes(data={"symbols": symbol})
                q = quote_resp.get("d", [{}])[0].get("v", {}) if isinstance(quote_resp, dict) else {}
                spot_price = float(q.get("lp", 0) or 0)
            except Exception:
                pass

        if not options_data:
            st.warning(
                "⚠️ No options data returned for this symbol. This can mean: "
                "the market is closed, the symbol/strike count combination is "
                "invalid, or the API response uses a different field name than "
                "expected. Enable **'Show raw API response'** in the sidebar and "
                "re-fetch to inspect the actual payload."
            )
            return

        df = normalize_chain_shape(options_data)

        num_cols = ["strike_price", "ce_ltp", "ce_oi", "ce_volume", "ce_chng_oi",
                    "pe_ltp", "pe_oi", "pe_volume", "pe_chng_oi"]
        for col in num_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
            else:
                df[col] = 0  # ensure downstream code doesn't KeyError

        df.sort_values("strike_price", inplace=True)
        df.reset_index(drop=True, inplace=True)
        df = compute_strike_bias(df)
        df = add_iv_columns(df, spot_price, selected_expiry_label)

        # Stash everything needed for redraw in session_state so switching
        # tabs / touching widgets doesn't wipe the dashboard back to empty.
        st.session_state["oc_df"] = df
        st.session_state["oc_spot"] = spot_price
        st.session_state["oc_symbol"] = symbol

    # ── Render from session_state (persists across reruns/tab switches) ────
    if "oc_df" not in st.session_state:
        st.info("👈 Choose an instrument in the sidebar and click **Fetch Live Data**.")
        return

    df = st.session_state["oc_df"]
    spot_price = st.session_state["oc_spot"]

    total_ce = df["ce_oi"].sum()
    total_pe = df["pe_oi"].sum()
    pcr = total_pe / total_ce if total_ce > 0 else 0
    max_pain = calculate_max_pain(df)

    if spot_price:
        atm_strike = df.iloc[(df["strike_price"] - spot_price).abs().argsort().iloc[:1]]["strike_price"].values[0]
    else:
        atm_strike = df["strike_price"].median()

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Spot Price", f"₹{spot_price:,.2f}" if spot_price else "—")
    c2.metric("ATM Strike", f"₹{atm_strike:,.0f}")
    c3.metric("Total CE OI", f"{total_ce/1e5:.1f}L")
    c4.metric("Total PE OI", f"{total_pe/1e5:.1f}L")
    c5.metric("Max Pain", f"₹{max_pain:,.0f}")

    st.markdown("<br>", unsafe_allow_html=True)

    # ── Big Move Alerts ──────────────────────────────────────────────────
    big_moves = detect_big_moves(df)
    if big_moves:
        st.markdown("**⚡ Big Move Alerts — Unusual OI Activity**")
        for alert in big_moves[:5]:
            badge = "🟢 BUY" if alert["direction"] == "BUY" else "🔴 SELL"
            box = st.success if alert["direction"] == "BUY" else st.error
            box(f"{badge} · Strike **{alert['strike']:,.0f}** ({alert['side']}) · "
                f"ΔOI {alert['oi_change']:+,.0f} — {alert['note']}")
        st.caption(
            "Based on unusual open-interest change (top percentile of ΔOI across strikes). "
            "This is a positioning signal, not financial advice — confirm with price action before acting."
        )
        st.markdown("<br>", unsafe_allow_html=True)

    sig_col, gauge_col = st.columns([1, 1])
    with sig_col:
        st.markdown("**Market Sentiment**")
        st.markdown(pcr_signal(pcr), unsafe_allow_html=True)
        st.markdown(f"<br>PCR = **{pcr:.3f}**  |  Max Pain = **{max_pain:,.0f}**", unsafe_allow_html=True)

        top_pe = df.loc[df["pe_oi"].idxmax(), "strike_price"]
        top_ce = df.loc[df["ce_oi"].idxmax(), "strike_price"]
        st.markdown(f"🛡️ Support (max PE OI): **{top_pe:,.0f}**")
        st.markdown(f"🧱 Resistance (max CE OI): **{top_ce:,.0f}**")

    with gauge_col:
        st.plotly_chart(pcr_gauge(pcr), use_container_width=True, config={"displayModeBar": False})

    st.divider()

    tab1, tab2, tab3 = st.tabs(["📋 Chain Table", "📊 OI Analysis", "📈 IV Skew"])

    with tab1:
        big_moves = df[df["Big Move"] == "🚨 Big Move"] if "Big Move" in df.columns else pd.DataFrame()
        if not big_moves.empty:
            buy_strikes = big_moves[big_moves["Strike Signal"] == "🟢 BUY"]["strike_price"].tolist()
            sell_strikes = big_moves[big_moves["Strike Signal"] == "🔴 SELL"]["strike_price"].tolist()
            parts = []
            if buy_strikes:
                parts.append(f"🟢 **Buy-side build-up:** {', '.join(f'{s:,.0f}' for s in buy_strikes)}")
            if sell_strikes:
                parts.append(f"🔴 **Sell-side build-up:** {', '.join(f'{s:,.0f}' for s in sell_strikes)}")
            if parts:
                st.markdown("🚨 **Big OI moves detected** — " + "  |  ".join(parts))

        display_df = style_chain_table(df)
        st.dataframe(
            display_df.style
                .background_gradient(subset=[c for c in ["CE OI", "PE OI"] if c in display_df.columns],
                                     cmap="RdYlGn", vmin=0)
                .format({c: "{:,.0f}" for c in display_df.select_dtypes("number").columns}),
            use_container_width=True,
            height=520,
        )

    with tab2:
        st.markdown("##### Open Interest — Calls vs Puts")
        st.plotly_chart(oi_bar_chart(df, max_pain), use_container_width=True,
                        config={"displayModeBar": False})

        st.markdown("**Top 5 CE OI Strikes**")
        top5_ce = df.nlargest(5, "ce_oi")[["strike_price", "ce_oi", "ce_ltp"]].reset_index(drop=True)
        st.dataframe(top5_ce.style.format({"ce_oi": "{:,.0f}", "ce_ltp": "{:.2f}"}),
                     use_container_width=True, height=215)

        st.markdown("**Top 5 PE OI Strikes**")
        top5_pe = df.nlargest(5, "pe_oi")[["strike_price", "pe_oi", "pe_ltp"]].reset_index(drop=True)
        st.dataframe(top5_pe.style.format({"pe_oi": "{:,.0f}", "pe_ltp": "{:.2f}"}),
                     use_container_width=True, height=215)

    with tab3:
        st.markdown("##### Implied Volatility Skew")
        st.plotly_chart(iv_chart(df), use_container_width=True,
                        config={"displayModeBar": False})
