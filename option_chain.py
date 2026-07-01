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

# ─── Custom CSS ─────────────────────────────────────────────────────────────
st.markdown("""
<style>
    .stApp { background-color: #0d1117; }
    section[data-testid="stSidebar"] { background-color: #161b22; border-right: 1px solid #30363d; }
    div[data-testid="metric-container"] {
        background: #161b22; border: 1px solid #30363d;
        border-radius: 8px; padding: 16px 20px;
    }
    div[data-testid="metric-container"] label { color: #8b949e !important; font-size: 12px; text-transform: uppercase; letter-spacing: 0.08em; }
    div[data-testid="metric-container"] div[data-testid="stMetricValue"] { color: #e6edf3 !important; font-size: 24px; font-weight: 700; font-family: 'Courier New', monospace; }
    h1, h2, h3 { color: #e6edf3 !important; }
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
def calculate_max_pain(df):
    strikes = df["strike_price"].values
    ce_oi, pe_oi = df["ce_oi"].values, df["pe_oi"].values
    pain = [np.sum(np.maximum(s - strikes, 0) * ce_oi) + np.sum(np.maximum(strikes - s, 0) * pe_oi) for s in strikes]
    return float(strikes[int(np.argmin(pain))])

def pcr_signal(pcr):
    if pcr > 1.3: return '<span class="signal-bull">🟢 Bullish (High PCR)</span>'
    elif pcr < 0.7: return '<span class="signal-bear">🔴 Bearish (Low PCR)</span>'
    return '<span class="signal-neu">🟡 Neutral</span>'

def oi_bar_chart(df, max_pain):
    fig = make_subplots(rows=1, cols=2, subplot_titles=("Call OI (CE)", "Put OI (PE)"),
                        shared_yaxes=True, horizontal_spacing=0.04)
    max_oi = max(df["ce_oi"].max(), df["pe_oi"].max())
    strikes_sorted = df["strike_price"].sort_values().unique()
    strike_gap = (strikes_sorted[1] - strikes_sorted[0]) if len(strikes_sorted) > 1 else 1
    fig.add_trace(go.Bar(x=-df["ce_oi"], y=df["strike_price"], orientation="h",
        marker_color=["#1a7f37" if abs(s - max_pain) < strike_gap / 2 else "#238636" for s in df["strike_price"]],
        name="CE OI", showlegend=False, customdata=df["ce_oi"],
        hovertemplate="Strike %{y}<br>CE OI: %{customdata:,}<extra></extra>"), row=1, col=1)
    fig.add_trace(go.Bar(x=df["pe_oi"], y=df["strike_price"], orientation="h",
        marker_color=["#b91c1c" if abs(s - max_pain) < strike_gap / 2 else "#da3633" for s in df["strike_price"]],
        name="PE OI", showlegend=False,
        hovertemplate="Strike %{y}<br>PE OI: %{x:,}<extra></extra>"), row=1, col=2)
    for col in [1, 2]:
        fig.add_hline(y=max_pain, line_dash="dot", line_color="#f0c814",
                      annotation_text=f"Max Pain {max_pain:,.0f}", annotation_font_color="#f0c814", row=1, col=col)
    fig.update_layout(paper_bgcolor="#0d1117", plot_bgcolor="#0d1117",
        font=dict(color="#8b949e", family="Courier New"), height=500, margin=dict(l=10, r=10, t=40, b=10),
        xaxis=dict(showticklabels=False, zeroline=False, showgrid=False, range=[-max_oi * 1.1, 0]),
        xaxis2=dict(showticklabels=False, zeroline=False, showgrid=False, range=[0, max_oi * 1.1]),
        yaxis=dict(showgrid=True, gridcolor="#21262d", tickfont=dict(color="#e6edf3", size=11)))
    fig.update_annotations(font_color="#8b949e")
    return fig

def pcr_gauge(pcr):
    fig = go.Figure(go.Indicator(
        mode="gauge+number", value=pcr,
        number={"font": {"color": "#e6edf3", "size": 36, "family": "Courier New"}},
        gauge={"axis": {"range": [0, 3], "tickcolor": "#8b949e", "tickfont": {"color": "#8b949e"}},
               "bar": {"color": "#58a6ff", "thickness": 0.25}, "bgcolor": "#161b22", "borderwidth": 0,
               "steps": [{"range": [0, 0.7], "color": "#3b0d1a"}, {"range": [0.7, 1.3], "color": "#1c2128"},
                          {"range": [1.3, 3.0], "color": "#0d3b2e"}],
               "threshold": {"line": {"color": "#f0c814", "width": 3}, "value": pcr}},
        title={"text": "PUT/CALL RATIO", "font": {"color": "#8b949e", "size": 12}},
        domain={"x": [0, 1], "y": [0, 1]}))
    fig.update_layout(paper_bgcolor="#0d1117", font=dict(color="#8b949e"), height=220, margin=dict(l=20, r=20, t=30, b=0))
    return fig

def iv_chart(df):
    fig = go.Figure()
    if "ce_iv" in df.columns:
        fig.add_trace(go.Scatter(x=df["strike_price"], y=df["ce_iv"], mode="lines+markers",
                                 name="CE IV", line=dict(color="#238636", width=2), marker=dict(size=5)))
    if "pe_iv" in df.columns:
        fig.add_trace(go.Scatter(x=df["strike_price"], y=df["pe_iv"], mode="lines+markers",
                                 name="PE IV", line=dict(color="#da3633", width=2), marker=dict(size=5)))
    if "ce_iv" not in df.columns and "pe_iv" not in df.columns:
        fig.add_annotation(text="IV data not available", xref="paper", yref="paper",
                           x=0.5, y=0.5, font=dict(color="#8b949e"), showarrow=False)
    fig.update_layout(paper_bgcolor="#0d1117", plot_bgcolor="#0d1117",
        font=dict(color="#8b949e", family="Courier New"), height=280, margin=dict(l=10, r=10, t=10, b=30),
        xaxis=dict(showgrid=True, gridcolor="#21262d", title="Strike"),
        yaxis=dict(showgrid=True, gridcolor="#21262d", title="IV %"),
        legend=dict(bgcolor="#161b22", bordercolor="#30363d", borderwidth=1))
    return fig

def style_chain_table(df):
    cols = ["ce_oi","ce_chng_oi","ce_volume","ce_ltp","CE Bias","strike_price",
            "PE Bias","pe_ltp","pe_volume","pe_chng_oi","pe_oi","Strike Signal","Big Move"]
    out = df[[c for c in cols if c in df.columns]].copy()
    out.rename(columns={"ce_oi":"CE OI","ce_chng_oi":"CE ΔOI","ce_volume":"CE Vol","ce_ltp":"CE LTP",
                         "strike_price":"Strike ⚡","pe_ltp":"PE LTP","pe_volume":"PE Vol",
                         "pe_chng_oi":"PE ΔOI","pe_oi":"PE OI"}, inplace=True)
    return out

def extract_options_data(response):
    data = response.get("data", {})
    for key in ("options", "optionsChain", "optionschain", "data"):
        candidate = data.get(key)
        if isinstance(candidate, list) and candidate:
            return candidate, data
    if isinstance(data, list) and data:
        return data, {}
    return [], data

def extract_spot_price(response, data):
    candidates = []
    if isinstance(data, dict):
        candidates += [data.get(k) for k in ("ltp","spot_price","spotPrice","underlyingValue",
                                               "underlying_value","underlyingLtp","underlying_ltp")]
    candidates += [response.get(k) for k in ("ltp","spot_price","spotPrice","underlyingValue","underlying_value")]
    for val in candidates:
        try:
            f = float(val)
            if f > 0: return f
        except (TypeError, ValueError):
            continue
    return 0.0

def extract_expiry_list(response):
    data = response.get("data", {}) if isinstance(response, dict) else {}
    raw = data.get("expiryData") or data.get("expirydata") or []
    out = []
    for item in raw:
        if not isinstance(item, dict): continue
        label = item.get("date") or item.get("expiry_date") or str(item.get("expiry", ""))
        ts = item.get("expiry") or item.get("timestamp")
        if ts is not None:
            out.append((label, str(ts)))
    return out

def normalize_chain_shape(options_data):
    raw = pd.DataFrame(options_data)
    if any(c.startswith("ce_") or c.startswith("pe_") for c in raw.columns):
        return raw
    type_col = next((c for c in ("option_type","optionType","type","instrument_type") if c in raw.columns), None)
    if type_col is None: return raw
    raw[type_col] = raw[type_col].astype(str).str.upper()
    field_map = {"oi":"oi","open_interest":"oi","ltp":"ltp","last_price":"ltp",
                 "volume":"volume","vol":"volume","chng_oi":"chng_oi","change_oi":"chng_oi",
                 "oi_change":"chng_oi","iv":"iv","implied_volatility":"iv"}
    raw = raw.rename(columns={k: v for k, v in field_map.items() if k in raw.columns})
    value_cols = [c for c in ("oi","ltp","volume","chng_oi","iv") if c in raw.columns]
    ce_df = raw[raw[type_col]=="CE"][["strike_price"]+value_cols].copy()
    pe_df = raw[raw[type_col]=="PE"][["strike_price"]+value_cols].copy()
    ce_df.rename(columns={c: f"ce_{c}" for c in value_cols}, inplace=True)
    pe_df.rename(columns={c: f"pe_{c}" for c in value_cols}, inplace=True)
    return pd.merge(ce_df, pe_df, on="strike_price", how="outer")

def compute_strike_bias(df):
    out = df.copy()
    ce_chng = out.get("ce_chng_oi", pd.Series(0, index=out.index))
    pe_chng = out.get("pe_chng_oi", pd.Series(0, index=out.index))
    out["CE Bias"] = ce_chng.apply(lambda v: "🔴 Sell Side (Call Writing)" if v > 0 else ("🟢 Unwinding" if v < 0 else "⚪ Flat"))
    out["PE Bias"] = pe_chng.apply(lambda v: "🟢 Buy Side (Put Writing)" if v > 0 else ("🔴 Unwinding" if v < 0 else "⚪ Flat"))
    out["_ce"] = ce_chng; out["_pe"] = pe_chng
    out["Strike Signal"] = out.apply(lambda r: "🟢 BUY" if r["_pe"] > 0 and r["_pe"] >= max(r["_ce"],0)
                                      else ("🔴 SELL" if r["_ce"] > 0 and r["_ce"] >= max(r["_pe"],0) else "🟡 NEUTRAL"), axis=1)
    mags = pd.concat([ce_chng.abs(), pe_chng.abs()])
    thresh = mags.quantile(0.8) if len(mags) > 0 and mags.max() > 0 else float("inf")
    out["Big Move"] = ((ce_chng.abs() >= thresh) | (pe_chng.abs() >= thresh)).map({True:"🚨 Big Move", False:""})
    out.drop(columns=["_ce","_pe"], inplace=True)
    return out

def detect_big_moves(df, top_n=3):
    alerts = []
    if "ce_chng_oi" not in df.columns or "pe_chng_oi" not in df.columns: return alerts
    ce_thresh = df["ce_chng_oi"].abs().quantile(0.85) if df["ce_chng_oi"].abs().max() > 0 else 0
    pe_thresh = df["pe_chng_oi"].abs().quantile(0.85) if df["pe_chng_oi"].abs().max() > 0 else 0
    for _, row in df.reindex(df["ce_chng_oi"].abs().sort_values(ascending=False).index).head(top_n).iterrows():
        chg = row["ce_chng_oi"]
        if abs(chg) < ce_thresh or chg == 0: continue
        alerts.append({"strike": row["strike_price"], "side": "CE",
                        "direction": "SELL" if chg > 0 else "BUY", "oi_change": chg,
                        "note": (f"Heavy CALL writing at {row['strike_price']:,.0f} — resistance building." if chg > 0
                                 else f"CALL OI unwinding at {row['strike_price']:,.0f} — resistance weakening.")})
    for _, row in df.reindex(df["pe_chng_oi"].abs().sort_values(ascending=False).index).head(top_n).iterrows():
        chg = row["pe_chng_oi"]
        if abs(chg) < pe_thresh or chg == 0: continue
        alerts.append({"strike": row["strike_price"], "side": "PE",
                        "direction": "BUY" if chg > 0 else "SELL", "oi_change": chg,
                        "note": (f"Heavy PUT writing at {row['strike_price']:,.0f} — support building." if chg > 0
                                 else f"PUT OI unwinding at {row['strike_price']:,.0f} — support weakening.")})
    alerts.sort(key=lambda a: abs(a["oi_change"]), reverse=True)
    return alerts

# ── Black-Scholes IV ─────────────────────────────────────────────────────────
def _norm_cdf(x): return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def _bs_price(spot, strike, t, r, sigma, is_call):
    if t <= 0 or sigma <= 0 or spot <= 0 or strike <= 0:
        return max(0.0, (spot - strike) if is_call else (strike - spot))
    d1 = (math.log(spot/strike) + (r + 0.5*sigma**2)*t) / (sigma*math.sqrt(t))
    d2 = d1 - sigma*math.sqrt(t)
    return (spot*_norm_cdf(d1) - strike*math.exp(-r*t)*_norm_cdf(d2) if is_call
            else strike*math.exp(-r*t)*_norm_cdf(-d2) - spot*_norm_cdf(-d1))

def implied_volatility(price, spot, strike, t, is_call, r=0.07):
    if price <= 0 or spot <= 0 or strike <= 0 or t <= 0: return 0.0
    sigma = 0.3
    for _ in range(50):
        d1 = (math.log(spot/strike) + (r + 0.5*sigma**2)*t) / (sigma*math.sqrt(t))
        vega = spot * math.exp(-0.5*d1**2) / math.sqrt(2*math.pi) * math.sqrt(t)
        diff = _bs_price(spot, strike, t, r, sigma, is_call) - price
        if abs(diff) < 1e-4 or vega < 1e-8: break
        sigma = max(0.001, min(sigma - diff/vega, 5.0))
    return round(sigma*100, 2)

def parse_days_to_expiry(label):
    if not label: return 7.0
    for fmt in ("%d-%m-%Y", "%d-%b-%Y", "%Y-%m-%d"):
        try:
            delta = (datetime.strptime(label, fmt) - datetime.now()).total_seconds() / 86400
            return max(delta, 0.5)
        except ValueError: continue
    return 7.0

def add_iv_columns(df, spot, expiry_label):
    if "ce_iv" in df.columns and "pe_iv" in df.columns and df["ce_iv"].abs().sum() > 0:
        return df
    if not spot: return df
    t = max(parse_days_to_expiry(expiry_label), 0.5) / 365.0
    df = df.copy()
    df["ce_iv"] = df.apply(lambda r: implied_volatility(r.get("ce_ltp",0), spot, r["strike_price"], t, True), axis=1)
    df["pe_iv"] = df.apply(lambda r: implied_volatility(r.get("pe_ltp",0), spot, r["strike_price"], t, False), axis=1)
    return df

# ── Symbol helpers ────────────────────────────────────────────────────────────
def normalize_symbol(stock, with_eq=False):
    stock = stock.strip().upper()
    if stock.endswith("-EQ"): stock = stock[:-3]
    return f"NSE:{stock}-EQ" if with_eq else f"NSE:{stock}"

def fetch_expiry_list(fyers, symbol, strikecount):
    """
    First-call: fetch WITHOUT a timestamp so Fyers returns the nearest
    expiry automatically AND provides the expiryData list in the response.
    We use this solely to populate the expiry dropdown — the actual chain
    data from this same response is also stored so the first fetch works.
    Never sends timestamp="" which causes API error code 1.
    """
    try:
        resp = fyers.optionchain(data={"symbol": symbol, "strikecount": int(strikecount)})
        return resp
    except Exception as e:
        return {"s": "error", "message": str(e)}

def fetch_optionchain_with_fallback(fyers, symbol, strikecount, is_stock, expiry_timestamp=""):
    """
    Sends timestamp ONLY when we actually have one (non-empty string).
    An empty string was previously being sent as timestamp:"" which Fyers
    rejects with API error code 1 ('Please provide valid expiry').
    """
    attempts = []
    tried_symbols = [symbol]
    if is_stock:
        alt = normalize_symbol(symbol.split(":")[-1], with_eq=not symbol.endswith("-EQ"))
        if alt not in tried_symbols:
            tried_symbols.append(alt)

    last_response = None
    for sym in tried_symbols:
        # KEY FIX: only include timestamp in the request when it is a
        # non-empty string. Sending timestamp="" triggers Fyers error code 1.
        req = {"symbol": sym, "strikecount": int(strikecount)}
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

# ── Big Move Scoring ──────────────────────────────────────────────────────────
def compute_big_move_scores(df, spot, pcr, max_pain):
    if len(df) == 0:
        return pd.DataFrame(columns=["Strike","Big Move Score","Direction","Reason"])
    ce_chng = df.get("ce_chng_oi", pd.Series(0, index=df.index))
    pe_chng = df.get("pe_chng_oi", pd.Series(0, index=df.index))
    ce_oi   = df.get("ce_oi",      pd.Series(0, index=df.index))
    pe_oi   = df.get("pe_oi",      pd.Series(0, index=df.index))
    ce_vol  = df.get("ce_volume",  pd.Series(0, index=df.index))
    pe_vol  = df.get("pe_volume",  pd.Series(0, index=df.index))

    max_abs_chng = max(ce_chng.abs().max(), pe_chng.abs().max(), 1)
    ce_chng_n = ce_chng.abs() / max_abs_chng
    pe_chng_n = pe_chng.abs() / max_abs_chng
    ce_oi_n   = ce_oi / max(ce_oi.max(), 1)
    pe_oi_n   = pe_oi / max(pe_oi.max(), 1)
    avg_cv = ce_vol.mean() if ce_vol.mean() > 0 else 1
    avg_pv = pe_vol.mean() if pe_vol.mean() > 0 else 1
    ce_vr = ce_vol / avg_cv; pe_vr = pe_vol / avg_pv
    vol_surge = (ce_vr >= 2) | (pe_vr >= 2)
    ce_conf = (ce_chng_n > 0.4) & (ce_vr >= 1.5)
    pe_conf = (pe_chng_n > 0.4) & (pe_vr >= 1.5)
    strike_gap = df["strike_price"].diff().abs().median() or 1
    dist = (df["strike_price"] - spot).abs() if spot else pd.Series(0, index=df.index)
    prox = (1 - (dist / (strike_gap * 5)).clip(0, 1))
    pcr_bias = "bullish" if pcr > 1.3 else ("bearish" if pcr < 0.7 else "neutral")

    rows = []
    for i in df.index:
        ce_c, pe_c = ce_chng.loc[i], pe_chng.loc[i]
        score = 0.0; reasons = []

        delta_pts = max(ce_chng_n.loc[i], pe_chng_n.loc[i]) * 25
        score += delta_pts
        if delta_pts > 12: reasons.append("Sharp ΔOI build-up")

        oi_pts = max(ce_oi_n.loc[i], pe_oi_n.loc[i]) * 20
        score += oi_pts
        if ce_oi_n.loc[i] >= 0.9: reasons.append("Highest CE OI (Resistance)")
        if pe_oi_n.loc[i] >= 0.9: reasons.append("Highest PE OI (Support)")

        vol_pts = 15 if vol_surge.loc[i] else min(max(ce_vr.loc[i], pe_vr.loc[i]) / 2, 1) * 15
        score += vol_pts
        if vol_surge.loc[i]: reasons.append("Volume > 2x average")

        if ce_conf.loc[i] or pe_conf.loc[i]:
            score += 20; reasons.append("OI + Volume confirmed build-up")

        prox_pts = prox.loc[i] * 10
        score += prox_pts
        if prox.loc[i] > 0.6: reasons.append("Near Spot (high break probability)")

        pcr_pts = 0
        if pcr_bias == "bullish" and pe_c > ce_c:
            pcr_pts = 10; reasons.append("Aligned with Bullish PCR")
        elif pcr_bias == "bearish" and ce_c > pe_c:
            pcr_pts = 10; reasons.append("Aligned with Bearish PCR")
        else:
            pcr_pts = 5
        score += pcr_pts
        score = round(min(score, 100), 1)

        if score >= 40:
            direction = "BUY" if (pe_c > 0 and pe_c >= ce_c) else ("SELL" if ce_c > 0 else "WAIT")
        else:
            direction = "WAIT"

        rows.append({"Strike": df.loc[i,"strike_price"], "Big Move Score": score,
                     "Direction": direction, "Reason": " + ".join(reasons[:3]) or "Mixed/weak signals",
                     "_ce_oi_n": ce_oi_n.loc[i], "_pe_oi_n": pe_oi_n.loc[i], "_prox": prox.loc[i]})

    return pd.DataFrame(rows).sort_values("Big Move Score", ascending=False).reset_index(drop=True)

def big_move_tier(score):
    if score >= 80: return "🔥 Very High"
    if score >= 60: return "🟢 High"
    if score >= 40: return "🟡 Medium"
    return "⚪ Low"

def build_recommendation(scored, spot, max_pain):
    if scored.empty: return {}
    buys  = scored[scored["Direction"] == "BUY"]
    sells = scored[scored["Direction"] == "SELL"]
    above = scored[scored["Strike"] > spot] if spot else scored
    below = scored[scored["Strike"] < spot] if spot else scored
    return {
        "best_buy":  buys.iloc[0]  if not buys.empty  else None,
        "best_sell": sells.iloc[0] if not sells.empty else None,
        "breakout":  above.iloc[0] if not above.empty else None,
        "breakdown": below.iloc[0] if not below.empty else None,
        "avoid":     scored[scored["Big Move Score"] < 40]["Strike"].tolist(),
    }

# ─── Main ────────────────────────────────────────────────────────────────────
def show_option_chain(fyers):
    st.markdown("## 📊 Master Options Chain Dashboard")
    st.markdown("<hr style='border-color:#30363d;margin:0 0 20px 0'>", unsafe_allow_html=True)

    symbol_map = {
        "NIFTY 50":     "NSE:NIFTY50-INDEX",
        "NIFTY BANK":   "NSE:NIFTYBANK-INDEX",
        "FINNIFTY":     "NSE:FINNIFTY-INDEX",
        "MIDCAP NIFTY": "NSE:MIDCPNIFTY-INDEX",
        "SENSEX":       "BSE:SENSEX-INDEX",
        "BANKEX":       "BSE:BANKEX-INDEX",
        "NIFTY NXT 50": "NSE:NIFTYNEXT50-INDEX",
    }

    with st.sidebar:
        st.markdown("### ⚙️ Configuration")
        option_type = st.radio("Instrument Type", ["Indices", "F&O Stocks"])
        is_stock = option_type == "F&O Stocks"
        if not is_stock:
            selected_key = st.selectbox("Index", list(symbol_map.keys()))
            symbol = symbol_map[selected_key]
        else:
            stock = st.text_input("Stock Symbol (e.g. RELIANCE)", "RELIANCE")
            symbol = normalize_symbol(stock)

        max_strikes = 20 if is_stock else 30
        strike_count = st.slider("Strikes Around ATM", 5, max_strikes, min(20, max_strikes), step=5)

        # ── Expiry dropdown ───────────────────────────────────────────────
        # Pre-fetch the expiry list for the current symbol when it hasn't
        # been loaded yet or when the symbol changes. This is a lightweight
        # call WITHOUT a timestamp so we avoid the code-1 error.
        cache_key = f"oc_expiry_{symbol}"
        if cache_key not in st.session_state:
            with st.spinner("Loading expiry dates…"):
                pre_resp = fetch_expiry_list(fyers, symbol, strike_count)
                expiry_list = extract_expiry_list(pre_resp)
                st.session_state[cache_key] = expiry_list
                # If this pre-fetch already returned a full chain, cache it
                # so the first "Fetch" click is instant.
                if isinstance(pre_resp, dict) and pre_resp.get("s") == "ok":
                    st.session_state["oc_prefetch"] = pre_resp

        expiry_options = st.session_state.get(cache_key, [])
        if expiry_options:
            expiry_labels = [label for label, _ in expiry_options]
            selected_expiry_label = st.selectbox("Expiry", expiry_labels)
            expiry_timestamp = dict(expiry_options).get(selected_expiry_label, "")
        else:
            st.caption("No expiry data yet — will use nearest expiry automatically.")
            selected_expiry_label = ""
            expiry_timestamp = ""

        debug_mode = st.checkbox("Show raw API response (debug)", value=False)
        st.divider()
        fetch_btn = st.button("🔄 Fetch Live Data", use_container_width=True, type="primary")

    # ── Fetch ────────────────────────────────────────────────────────────────
    if fetch_btn:
        with st.spinner("Connecting to Fyers API…"):
            response, used_symbol, attempts = fetch_optionchain_with_fallback(
                fyers, symbol, strike_count, is_stock, expiry_timestamp
            )

        if debug_mode:
            st.write("**Symbols tried:**", attempts)
            st.json(response)

        if not response:
            st.error("API call failed. Check your Fyers connection/token.")
            return

        if response.get("s") != "ok":
            err_code = response.get("code", "—")
            err_msg  = response.get("message", "No data returned")
            st.error(f"API Error (code {err_code}): {err_msg}\n\n"
                     f"Tried: {', '.join(s for s, _ in attempts)}.")
            return

        # Update expiry list from this response
        new_expiry = extract_expiry_list(response)
        if new_expiry:
            st.session_state[cache_key] = new_expiry

        options_data, data = extract_options_data(response)
        spot_price = extract_spot_price(response, data)

        if not spot_price:
            try:
                q = fyers.quotes(data={"symbols": used_symbol})
                spot_price = float((q.get("d",[{}])[0].get("v",{}) or {}).get("lp", 0) or 0)
            except Exception:
                pass

        if not options_data:
            st.warning("⚠️ No options data in response. Market may be closed or symbol invalid. "
                       "Enable debug mode and re-fetch to inspect the raw payload.")
            return

        df = normalize_chain_shape(options_data)
        for col in ["strike_price","ce_ltp","ce_oi","ce_volume","ce_chng_oi","pe_ltp","pe_oi","pe_volume","pe_chng_oi"]:
    if col in df.columns:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
    else:
        df[col] = 0 #
            df[col] = pd.to_numeric(df.get(col, 0), errors="coerce").fillna(0)
        df.sort_values("strike_price", inplace=True)
        df.reset_index(drop=True, inplace=True)
        df = compute_strike_bias(df)
        df = add_iv_columns(df, spot_price, selected_expiry_label)

        st.session_state.update({"oc_df": df, "oc_spot": spot_price, "oc_symbol": used_symbol})

    # ── Render ───────────────────────────────────────────────────────────────
    if "oc_df" not in st.session_state:
        st.info("👈 Choose an instrument in the sidebar and click **Fetch Live Data**.")
        return

    df         = st.session_state["oc_df"]
    spot_price = st.session_state["oc_spot"]
    total_ce   = df["ce_oi"].sum()
    total_pe   = df["pe_oi"].sum()
    pcr        = total_pe / total_ce if total_ce > 0 else 0
    max_pain   = calculate_max_pain(df)
    atm_strike = (df.iloc[(df["strike_price"] - spot_price).abs().argsort().iloc[:1]]["strike_price"].values[0]
                  if spot_price else df["strike_price"].median())

    c1,c2,c3,c4,c5 = st.columns(5)
    c1.metric("Spot Price",  f"₹{spot_price:,.2f}" if spot_price else "—")
    c2.metric("ATM Strike",  f"₹{atm_strike:,.0f}")
    c3.metric("Total CE OI", f"{total_ce/1e5:.1f}L")
    c4.metric("Total PE OI", f"{total_pe/1e5:.1f}L")
    c5.metric("Max Pain",    f"₹{max_pain:,.0f}")
    st.markdown("<br>", unsafe_allow_html=True)

    # Big move quick alerts
    big_moves = detect_big_moves(df)
    if big_moves:
        st.markdown("**⚡ Big Move Alerts — Unusual OI Activity**")
        for alert in big_moves[:5]:
            (st.success if alert["direction"]=="BUY" else st.error)(
                f"{'🟢 BUY' if alert['direction']=='BUY' else '🔴 SELL'} • "
                f"Strike **{alert['strike']:,.0f}** ({alert['side']}) • "
                f"ΔOI {alert['oi_change']:+,.0f} — {alert['note']}")
        st.caption("Positioning signal only — confirm with price action before acting.")
        st.markdown("<br>", unsafe_allow_html=True)

    sig_col, gauge_col = st.columns(2)
    with sig_col:
        st.markdown("**Market Sentiment**")
        st.markdown(pcr_signal(pcr), unsafe_allow_html=True)
        st.markdown(f"<br>PCR = **{pcr:.3f}**  |  Max Pain = **{max_pain:,.0f}**", unsafe_allow_html=True)
        st.markdown(f"🛡️ Support (max PE OI): **{df.loc[df['pe_oi'].idxmax(),'strike_price']:,.0f}**")
        st.markdown(f"🧱 Resistance (max CE OI): **{df.loc[df['ce_oi'].idxmax(),'strike_price']:,.0f}**")
    with gauge_col:
        st.plotly_chart(pcr_gauge(pcr), use_container_width=True, config={"displayModeBar": False})

    st.divider()
    tab1, tab2, tab3, tab4 = st.tabs(["📋 Chain Table", "📊 OI Analysis", "📈 IV Skew", "🔥 Big Move Analysis"])

    with tab1:
        bm = df[df.get("Big Move","") == "🚨 Big Move"] if "Big Move" in df.columns else pd.DataFrame()
        if not bm.empty:
            buy_s  = bm[bm["Strike Signal"]=="🟢 BUY"]["strike_price"].tolist()
            sell_s = bm[bm["Strike Signal"]=="🔴 SELL"]["strike_price"].tolist()
            parts  = []
            if buy_s:  parts.append(f"🟢 **Buy-side:** {', '.join(f'{s:,.0f}' for s in buy_s)}")
            if sell_s: parts.append(f"🔴 **Sell-side:** {', '.join(f'{s:,.0f}' for s in sell_s)}")
            if parts: st.markdown("🚨 **Big OI moves detected** — " + "  |  ".join(parts))
        disp = style_chain_table(df)
        st.dataframe(disp.style
            .background_gradient(subset=[c for c in ["CE OI","PE OI"] if c in disp.columns], cmap="RdYlGn", vmin=0)
            .format({c: "{:,.0f}" for c in disp.select_dtypes("number").columns}),
            use_container_width=True, height=520)

    with tab2:
        st.markdown("##### Open Interest — Calls vs Puts")
        st.plotly_chart(oi_bar_chart(df, max_pain), use_container_width=True, config={"displayModeBar": False})
        st.markdown("**Top 5 CE OI Strikes**")
        st.dataframe(df.nlargest(5,"ce_oi")[["strike_price","ce_oi","ce_ltp"]].reset_index(drop=True)
                     .style.format({"ce_oi":"{:,.0f}","ce_ltp":"{:.2f}"}), use_container_width=True, height=215)
        st.markdown("**Top 5 PE OI Strikes**")
        st.dataframe(df.nlargest(5,"pe_oi")[["strike_price","pe_oi","pe_ltp"]].reset_index(drop=True)
                     .style.format({"pe_oi":"{:,.0f}","pe_ltp":"{:.2f}"}), use_container_width=True, height=215)

    with tab3:
        st.markdown("##### Implied Volatility Skew")
        st.plotly_chart(iv_chart(df), use_container_width=True, config={"displayModeBar": False})

    with tab4:
        st.markdown("##### 🔥 Big Move Ready Strike Analysis — 7-Point Scoring")
        st.caption("Each strike scored 0–100 across: ΔOI surge • OI level • Volume • OI+Vol confirmation • "
                   "Spot proximity • PCR alignment • Max Pain distance. Positioning signal only.")

        scored = compute_big_move_scores(df, spot_price, pcr, max_pain)
        if scored.empty:
            st.info("No data — fetch the chain first.")
        else:
            scored["Probability"] = scored["Big Move Score"].apply(big_move_tier)
            show = scored[["Strike","Big Move Score","Probability","Direction","Reason"]].copy()
            show["Strike"] = show["Strike"].apply(lambda x: f"{x:,.0f}")
            show["Big Move Score"] = show["Big Move Score"].apply(lambda x: f"{x:.0f}")

            def row_color(row):
                s = scored.loc[row.name, "Big Move Score"]
                bg = "#3b0d1a" if s>=80 else ("#0d3b2e" if s>=60 else ("#1c2128" if s>=40 else "#161b22"))
                return [f"background-color: {bg}"] * len(row)

            st.dataframe(show.style.apply(row_color, axis=1), use_container_width=True, height=520)

            st.markdown("---")
            st.markdown("##### ✅ Final Recommendation")
            rec = build_recommendation(scored, spot_price, max_pain)
            c1, c2 = st.columns(2)
            with c1:
                b = rec.get("best_buy")
                st.success(f"**✅ Best BUY Strike:** {b['Strike']:,.0f}  (Score {b['Big Move Score']:.0f})\n\n{b['Reason']}"
                           if b is not None else "**Best BUY Strike:** No qualifying strike")
                bo = rec.get("breakout")
                st.warning(f"**⬆️ Breakout Strike (above spot):** {bo['Strike']:,.0f}  (Score {bo['Big Move Score']:.0f})"
                           if bo is not None else "**Breakout Strike:** N/A")
            with c2:
                s = rec.get("best_sell")
                st.error(f"**✅ Best SELL Strike:** {s['Strike']:,.0f}  (Score {s['Big Move Score']:.0f})\n\n{s['Reason']}"
                         if s is not None else "**Best SELL Strike:** No qualifying strike")
                bd = rec.get("breakdown")
                st.warning(f"**⬇️ Breakdown Strike (below spot):** {bd['Strike']:,.0f}  (Score {bd['Big Move Score']:.0f})"
                           if bd is not None else "**Breakdown Strike:** N/A")

            avoid = rec.get("avoid", [])
            if avoid:
                st.caption(f"⚪ **Avoid (score < 40):** {', '.join(f'{s:,.0f}' for s in avoid[:10])}")

            st.caption("Not financial advice. Confirm with price action and your own risk management.")
