import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import numpy as np
import math
import io
from datetime import datetime
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

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
    button[data-baseweb="tab"] { color: #8b949e !important; }
    button[data-baseweb="tab"][aria-selected="true"] { color: #58a6ff !important; border-bottom: 2px solid #58a6ff; }
    .stDataFrame { border: 1px solid #30363d; border-radius: 8px; overflow: hidden; }
    hr { border-color: #30363d; }
    .signal-bull { background: #0d3b2e; color: #3fb950; border: 1px solid #238636; padding: 4px 14px; border-radius: 20px; font-size: 13px; font-weight: 700; display: inline-block; }
    .signal-bear { background: #3b0d1a; color: #f85149; border: 1px solid #da3633; padding: 4px 14px; border-radius: 20px; font-size: 13px; font-weight: 700; display: inline-block; }
    .signal-neu  { background: #1c2128; color: #d29922; border: 1px solid #9e6a03; padding: 4px 14px; border-radius: 20px; font-size: 13px; font-weight: 700; display: inline-block; }
</style>
""", unsafe_allow_html=True)

# ─── Symbol Map ─────────────────────────────────────────────────────────────
# Fyers requires exact exchange-prefix:symbol format.
# BSE indices use BSE: prefix; NSE indices use NSE: prefix.
# NIFTYNEXT50 and BANKEX had wrong symbols in the original — corrected here.
SYMBOL_MAP = {
    "NIFTY 50":       "NSE:NIFTY50-INDEX",
    "NIFTY BANK":     "NSE:NIFTYBANK-INDEX",
    "FINNIFTY":       "NSE:FINNIFTY-INDEX",
    "MIDCAP NIFTY":   "NSE:MIDCPNIFTY-INDEX",
    "NIFTY NXT 50":   "NSE:NIFTYNXT50-INDEX",   # fixed: was NIFTYNEXT50
    "SENSEX":         "BSE:SENSEX-INDEX",
    "BANKEX":         "BSE:BANKEX-INDEX",
}

# ─── Helpers ────────────────────────────────────────────────────────────────
def calculate_max_pain(df):
    strikes = df["strike_price"].values
    ce_oi   = df["ce_oi"].values
    pe_oi   = df["pe_oi"].values
    pain = [np.sum(np.maximum(s - strikes, 0) * ce_oi) +
            np.sum(np.maximum(strikes - s, 0) * pe_oi) for s in strikes]
    return float(strikes[int(np.argmin(pain))])


def pcr_signal(pcr):
    if pcr > 1.3: return '<span class="signal-bull">🟢 Bullish (High PCR)</span>'
    if pcr < 0.7: return '<span class="signal-bear">🔴 Bearish (Low PCR)</span>'
    return '<span class="signal-neu">🟡 Neutral</span>'


def normalize_symbol(stock, with_eq=False):
    stock = stock.strip().upper()
    if stock.endswith("-EQ"): stock = stock[:-3]
    return f"NSE:{stock}-EQ" if with_eq else f"NSE:{stock}"


def fetch_optionchain_with_fallback(fyers, symbol, strikecount, is_stock, expiry_timestamp=""):
    tried, attempts, last_resp = [symbol], [], None
    if is_stock:
        alt = normalize_symbol(symbol.split(":")[-1], with_eq=not symbol.endswith("-EQ"))
        if alt not in tried: tried.append(alt)
    for sym in tried:
        req = {"symbol": sym, "strikecount": int(strikecount)}
        if expiry_timestamp: req["timestamp"] = expiry_timestamp
        try:
            resp = fyers.optionchain(data=req)
        except Exception as e:
            attempts.append((sym, f"exception: {e}")); continue
        attempts.append((sym, resp.get("s") if isinstance(resp, dict) else "no response"))
        last_resp = resp
        if isinstance(resp, dict) and resp.get("s") == "ok":
            return resp, sym, attempts
    return last_resp, tried[-1], attempts


def extract_options_data(response):
    data = response.get("data", {})
    for key in ("options", "optionsChain", "optionschain", "data"):
        c = data.get(key)
        if isinstance(c, list) and c: return c, data
    if isinstance(data, list) and data: return data, {}
    return [], data


def extract_spot_price(response, data):
    keys = ["ltp","spot_price","spotPrice","underlyingValue","underlying_value","underlyingLtp","underlying_ltp"]
    for src in ([data] if isinstance(data, dict) else []) + [response]:
        for k in keys:
            try:
                f = float(src.get(k))
                if f > 0: return f
            except (TypeError, ValueError): pass
    return 0.0


def extract_expiry_list(response):
    data = response.get("data", {}) if isinstance(response, dict) else {}
    raw  = data.get("expiryData") or data.get("expirydata") or []
    out  = []
    for item in raw:
        if not isinstance(item, dict): continue
        label = item.get("date") or item.get("expiry_date") or str(item.get("expiry",""))
        ts    = item.get("expiry") or item.get("timestamp")
        if ts is not None: out.append((label, str(ts)))
    return out


def normalize_chain_shape(options_data):
    raw = pd.DataFrame(options_data)
    if any(c.startswith("ce_") or c.startswith("pe_") for c in raw.columns):
        return raw
    type_col = next((c for c in ("option_type","optionType","type","instrument_type") if c in raw.columns), None)
    if not type_col: return raw
    raw[type_col] = raw[type_col].astype(str).str.upper()
    field_map = {"oi":"oi","open_interest":"oi","ltp":"ltp","last_price":"ltp",
                 "volume":"volume","vol":"volume","chng_oi":"chng_oi",
                 "change_oi":"chng_oi","oi_change":"chng_oi","iv":"iv","implied_volatility":"iv"}
    raw = raw.rename(columns={k:v for k,v in field_map.items() if k in raw.columns})
    vcols = [c for c in ("oi","ltp","volume","chng_oi","iv") if c in raw.columns]
    ce = raw[raw[type_col]=="CE"][["strike_price"]+vcols].copy().rename(columns={c:f"ce_{c}" for c in vcols})
    pe = raw[raw[type_col]=="PE"][["strike_price"]+vcols].copy().rename(columns={c:f"pe_{c}" for c in vcols})
    return pd.merge(ce, pe, on="strike_price", how="outer")


def compute_strike_bias(df):
    out    = df.copy()
    ce_chg = out.get("ce_chng_oi", pd.Series(0, index=out.index))
    pe_chg = out.get("pe_chng_oi", pd.Series(0, index=out.index))
    out["CE Bias"] = ce_chg.apply(lambda v: "🔴 Call Writing" if v>0 else ("🟢 Unwinding" if v<0 else "⚪ Flat"))
    out["PE Bias"]  = pe_chg.apply(lambda v: "🟢 Put Writing"  if v>0 else ("🔴 Unwinding" if v<0 else "⚪ Flat"))
    out["_ce"] = ce_chg; out["_pe"] = pe_chg
    def sig(row):
        if row["_pe"]>0 and row["_pe"]>=max(row["_ce"],0): return "🟢 BUY"
        if row["_ce"]>0 and row["_ce"]>=max(row["_pe"],0): return "🔴 SELL"
        return "🟡 NEUTRAL"
    out["Strike Signal"] = out.apply(sig, axis=1)
    mag = pd.concat([ce_chg.abs(), pe_chg.abs()])
    thr = mag.quantile(0.8) if mag.max()>0 else float("inf")
    out["Big Move"] = ((ce_chg.abs()>=thr)|(pe_chg.abs()>=thr)).map({True:"🚨 Big Move",False:""})
    out.drop(columns=["_ce","_pe"], inplace=True)
    return out


# ─── IV via Black-Scholes ───────────────────────────────────────────────────
def _norm_cdf(x): return 0.5*(1.0+math.erf(x/math.sqrt(2.0)))

def _bs_price(spot, strike, t, r, sigma, call):
    if t<=0 or sigma<=0 or spot<=0 or strike<=0:
        return max(0.0, (spot-strike) if call else (strike-spot))
    d1 = (math.log(spot/strike)+(r+0.5*sigma**2)*t)/(sigma*math.sqrt(t))
    d2 = d1 - sigma*math.sqrt(t)
    if call: return spot*_norm_cdf(d1) - strike*math.exp(-r*t)*_norm_cdf(d2)
    return strike*math.exp(-r*t)*_norm_cdf(-d2) - spot*_norm_cdf(-d1)

def implied_volatility(price, spot, strike, t, call, r=0.07):
    if price<=0 or spot<=0 or strike<=0 or t<=0: return 0.0
    sigma = 0.3
    for _ in range(50):
        mp   = _bs_price(spot, strike, t, r, sigma, call)
        d1   = (math.log(spot/strike)+(r+0.5*sigma**2)*t)/(sigma*math.sqrt(t))
        vega = spot*math.exp(-0.5*d1**2)/math.sqrt(2*math.pi)*math.sqrt(t)
        diff = mp - price
        if abs(diff)<1e-4 or vega<1e-8: break
        sigma -= diff/vega
        sigma  = max(0.001, min(sigma, 5.0))
    return round(sigma*100, 2)

def parse_dte(label):
    if not label: return 7.0
    for fmt in ("%d-%m-%Y","%d-%b-%Y","%Y-%m-%d"):
        try:
            d = (datetime.strptime(label, fmt)-datetime.now()).total_seconds()/86400
            return max(d, 0.5)
        except ValueError: pass
    return 7.0

def add_iv_columns(df, spot, expiry_label):
    if "ce_iv" in df.columns and "pe_iv" in df.columns and df["ce_iv"].abs().sum()>0:
        return df
    t = max(parse_dte(expiry_label), 0.5)/365.0
    if not spot: return df
    df = df.copy()
    df["ce_iv"] = df.apply(lambda r: implied_volatility(r.get("ce_ltp",0), spot, r["strike_price"], t, True), axis=1)
    df["pe_iv"] = df.apply(lambda r: implied_volatility(r.get("pe_ltp",0), spot, r["strike_price"], t, False), axis=1)
    return df


# ─── Big Move 7-Point Scoring ───────────────────────────────────────────────
def compute_big_move_scores(df, spot, pcr, max_pain):
    if df.empty: return pd.DataFrame()
    ce_chg = df.get("ce_chng_oi", pd.Series(0, index=df.index))
    pe_chg = df.get("pe_chng_oi", pd.Series(0, index=df.index))
    ce_oi  = df.get("ce_oi",      pd.Series(0, index=df.index))
    pe_oi  = df.get("pe_oi",      pd.Series(0, index=df.index))
    ce_vol = df.get("ce_volume",  pd.Series(0, index=df.index))
    pe_vol = df.get("pe_volume",  pd.Series(0, index=df.index))

    # normalise
    max_chg = max(ce_chg.abs().max(), pe_chg.abs().max(), 1)
    ce_cn   = ce_chg.abs()/max_chg; pe_cn = pe_chg.abs()/max_chg
    ce_on   = ce_oi/max(ce_oi.max(),1); pe_on = pe_oi/max(pe_oi.max(),1)
    avg_cv  = max(ce_vol.mean(), 1);   avg_pv = max(pe_vol.mean(), 1)
    cv_rat  = ce_vol/avg_cv;           pv_rat = pe_vol/avg_pv
    vol_srg = (cv_rat>=2)|(pv_rat>=2)
    ce_conf = (ce_cn>0.4)&(cv_rat>=1.5)
    pe_conf = (pe_cn>0.4)&(pv_rat>=1.5)
    gap     = max(df["strike_price"].diff().abs().median(), 1)
    dist    = (df["strike_price"]-spot).abs() if spot else pd.Series(0, index=df.index)
    prox    = (1-(dist/(gap*5)).clip(0,1))
    pcr_bias = "bull" if pcr>1.3 else ("bear" if pcr<0.7 else "neu")

    rows = []
    for i in df.index:
        s    = df.loc[i,"strike_price"]
        cc, pc = ce_chg.loc[i], pe_chg.loc[i]
        reasons = []

        # 1. ΔOI surge — 25 pts
        d1 = max(ce_cn.loc[i], pe_cn.loc[i])*25
        if d1>12: reasons.append("Sharp ΔOI surge")

        # 2. OI level — 20 pts
        d2 = max(ce_on.loc[i], pe_on.loc[i])*20
        if ce_on.loc[i]>=0.9: reasons.append("Max CE OI (Resistance)")
        if pe_on.loc[i]>=0.9: reasons.append("Max PE OI (Support)")

        # 3. Volume — 15 pts
        d3 = 15 if vol_srg.loc[i] else min(max(cv_rat.loc[i],pv_rat.loc[i])/2,1)*15
        if vol_srg.loc[i]: reasons.append("Volume >2x avg")

        # 4. OI + Volume confirmation — 20 pts
        d4 = 20 if (ce_conf.loc[i] or pe_conf.loc[i]) else 0
        if d4: reasons.append("OI+Volume confirmed")

        # 5. Proximity to spot — 10 pts
        d5 = prox.loc[i]*10
        if prox.loc[i]>0.6: reasons.append("Near Spot (break risk)")

        # 6. PCR + Max Pain alignment — 10 pts
        if pcr_bias=="bull" and pc>cc:   d6,r6 = 10,"Bullish PCR aligned"
        elif pcr_bias=="bear" and cc>pc: d6,r6 = 10,"Bearish PCR aligned"
        else:                             d6,r6 =  5,"Neutral PCR"
        reasons.append(r6)

        score = round(min(d1+d2+d3+d4+d5+d6, 100), 1)

        if score>=40:
            direction = "BUY"  if (pc>0 and pc>=cc) else ("SELL" if (cc>0 and cc>=pc) else "WAIT")
        else:
            direction = "WAIT"

        rows.append({
            "Strike": s, "Big Move Score": score, "Direction": direction,
            "Reason": " + ".join(reasons[:3]),
            "_ce_on": ce_on.loc[i], "_pe_on": pe_on.loc[i], "_prox": prox.loc[i],
        })

    return pd.DataFrame(rows).sort_values("Big Move Score", ascending=False).reset_index(drop=True)


def score_tier(score):
    if score>=80: return "🔥 Very High"
    if score>=60: return "🟢 High"
    if score>=40: return "🟡 Medium"
    return "⚪ Low"


def build_recommendations(scored, spot, max_pain):
    if scored.empty: return {}
    buys  = scored[scored["Direction"]=="BUY"]
    sells = scored[scored["Direction"]=="SELL"]
    above = scored[scored["Strike"]>spot] if spot else scored
    below = scored[scored["Strike"]<spot] if spot else scored
    return {
        "best_buy":   buys.iloc[0]  if not buys.empty  else None,
        "best_sell":  sells.iloc[0] if not sells.empty else None,
        "breakout":   above.iloc[0] if not above.empty else None,
        "breakdown":  below.iloc[0] if not below.empty else None,
        "avoid":      scored[scored["Big Move Score"]<40]["Strike"].tolist(),
    }


# ─── Detect Big Move Alerts ──────────────────────────────────────────────────
def detect_big_moves(df, top_n=3):
    alerts = []
    if "ce_chng_oi" not in df.columns or "pe_chng_oi" not in df.columns: return alerts
    ce_thr = df["ce_chng_oi"].abs().quantile(0.85) if df["ce_chng_oi"].abs().max()>0 else 0
    pe_thr = df["pe_chng_oi"].abs().quantile(0.85) if df["pe_chng_oi"].abs().max()>0 else 0
    for side, col, thr in [("CE","ce_chng_oi",ce_thr),("PE","pe_chng_oi",pe_thr)]:
        for _, row in df.reindex(df[col].abs().sort_values(ascending=False).index).head(top_n).iterrows():
            chg = row[col]
            if abs(chg)<thr or chg==0: continue
            if side=="CE":
                direction = "SELL" if chg>0 else "BUY"
                note = (f"Heavy CALL writing at {row['strike_price']:,.0f} — resistance building."
                        if chg>0 else
                        f"CALL OI unwinding at {row['strike_price']:,.0f} — possible breakout.")
            else:
                direction = "BUY" if chg>0 else "SELL"
                note = (f"Heavy PUT writing at {row['strike_price']:,.0f} — support building."
                        if chg>0 else
                        f"PUT OI unwinding at {row['strike_price']:,.0f} — possible breakdown.")
            alerts.append({"strike":row["strike_price"],"side":side,"direction":direction,"oi_change":chg,"note":note})
    alerts.sort(key=lambda a: abs(a["oi_change"]), reverse=True)
    return alerts


# ─── Charts ─────────────────────────────────────────────────────────────────
def oi_bar_chart(df, max_pain):
    fig = make_subplots(rows=1, cols=2, subplot_titles=("Call OI (CE)","Put OI (PE)"),
                        shared_yaxes=True, horizontal_spacing=0.04)
    max_oi = max(df["ce_oi"].max(), df["pe_oi"].max())
    srt = df["strike_price"].sort_values().unique()
    gap = (srt[1]-srt[0]) if len(srt)>1 else 1

    def color(s, pos): return ("#1a7f37" if pos else "#b91c1c") if abs(s-max_pain)<gap/2 else ("#238636" if pos else "#da3633")

    fig.add_trace(go.Bar(x=-df["ce_oi"], y=df["strike_price"], orientation="h",
                         marker_color=[color(s,True) for s in df["strike_price"]],
                         name="CE OI", showlegend=False, customdata=df["ce_oi"],
                         hovertemplate="Strike %{y}<br>CE OI: %{customdata:,}<extra></extra>"), row=1, col=1)
    fig.add_trace(go.Bar(x=df["pe_oi"], y=df["strike_price"], orientation="h",
                         marker_color=[color(s,False) for s in df["strike_price"]],
                         name="PE OI", showlegend=False,
                         hovertemplate="Strike %{y}<br>PE OI: %{x:,}<extra></extra>"), row=1, col=2)
    for c in [1,2]:
        fig.add_hline(y=max_pain, line_dash="dot", line_color="#f0c814",
                      annotation_text=f"Max Pain {max_pain:,.0f}", annotation_font_color="#f0c814", row=1, col=c)
    fig.update_layout(paper_bgcolor="#0d1117", plot_bgcolor="#0d1117",
                      font=dict(color="#8b949e", family="Courier New"), height=500,
                      margin=dict(l=10,r=10,t=40,b=10),
                      xaxis=dict(showticklabels=False, zeroline=False, showgrid=False, range=[-max_oi*1.1,0]),
                      xaxis2=dict(showticklabels=False, zeroline=False, showgrid=False, range=[0,max_oi*1.1]),
                      yaxis=dict(showgrid=True, gridcolor="#21262d", tickfont=dict(color="#e6edf3",size=11)))
    fig.update_annotations(font_color="#8b949e")
    return fig


def pcr_gauge(pcr):
    fig = go.Figure(go.Indicator(
        mode="gauge+number", value=pcr,
        number={"font":{"color":"#e6edf3","size":36,"family":"Courier New"}},
        gauge={"axis":{"range":[0,3],"tickcolor":"#8b949e","tickfont":{"color":"#8b949e"}},
               "bar":{"color":"#58a6ff","thickness":0.25},"bgcolor":"#161b22","borderwidth":0,
               "steps":[{"range":[0,0.7],"color":"#3b0d1a"},{"range":[0.7,1.3],"color":"#1c2128"},
                         {"range":[1.3,3.0],"color":"#0d3b2e"}],
               "threshold":{"line":{"color":"#f0c814","width":3},"value":pcr}},
        title={"text":"PUT/CALL RATIO","font":{"color":"#8b949e","size":12}},
        domain={"x":[0,1],"y":[0,1]}))
    fig.update_layout(paper_bgcolor="#0d1117", font=dict(color="#8b949e"), height=220, margin=dict(l=20,r=20,t=30,b=0))
    return fig


def iv_chart(df):
    fig = go.Figure()
    if "ce_iv" in df.columns:
        fig.add_trace(go.Scatter(x=df["strike_price"], y=df["ce_iv"], mode="lines+markers",
                                 name="CE IV", line=dict(color="#238636",width=2), marker=dict(size=5)))
    if "pe_iv" in df.columns:
        fig.add_trace(go.Scatter(x=df["strike_price"], y=df["pe_iv"], mode="lines+markers",
                                 name="PE IV", line=dict(color="#da3633",width=2), marker=dict(size=5)))
    if "ce_iv" not in df.columns and "pe_iv" not in df.columns:
        fig.add_annotation(text="IV data not available", xref="paper", yref="paper",
                           x=0.5, y=0.5, font=dict(color="#8b949e"), showarrow=False)
    fig.update_layout(paper_bgcolor="#0d1117", plot_bgcolor="#0d1117",
                      font=dict(color="#8b949e",family="Courier New"), height=280,
                      margin=dict(l=10,r=10,t=10,b=30),
                      xaxis=dict(showgrid=True,gridcolor="#21262d",title="Strike"),
                      yaxis=dict(showgrid=True,gridcolor="#21262d",title="IV %"),
                      legend=dict(bgcolor="#161b22",bordercolor="#30363d",borderwidth=1))
    return fig


def big_move_score_chart(scored):
    colors = ["#ff6b35" if s>=80 else "#3fb950" if s>=60 else "#d29922" if s>=40 else "#8b949e"
              for s in scored["Big Move Score"]]
    fig = go.Figure(go.Bar(
        x=scored["Strike"].apply(lambda x: f"{x:,.0f}"),
        y=scored["Big Move Score"],
        marker_color=colors,
        text=scored["Big Move Score"].apply(lambda x: f"{x:.0f}"),
        textposition="outside",
        hovertemplate="Strike %{x}<br>Score: %{y}<extra></extra>",
    ))
    fig.update_layout(paper_bgcolor="#0d1117", plot_bgcolor="#0d1117",
                      font=dict(color="#8b949e", family="Courier New"), height=350,
                      margin=dict(l=10,r=10,t=30,b=10),
                      xaxis=dict(showgrid=False, tickfont=dict(color="#e6edf3",size=10)),
                      yaxis=dict(showgrid=True, gridcolor="#21262d", range=[0,110],
                                 tickfont=dict(color="#8b949e")),
                      title=dict(text="Big Move Score per Strike", font=dict(color="#58a6ff",size=13)))
    fig.add_hline(y=80, line_dash="dot", line_color="#ff6b35", annotation_text="🔥 80", annotation_font_color="#ff6b35")
    fig.add_hline(y=60, line_dash="dot", line_color="#3fb950", annotation_text="🟢 60", annotation_font_color="#3fb950")
    fig.add_hline(y=40, line_dash="dot", line_color="#d29922", annotation_text="🟡 40", annotation_font_color="#d29922")
    return fig


# ─── Excel Export ────────────────────────────────────────────────────────────
def to_excel_bytes(chain_df, scored_df, spot, pcr, max_pain):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        # ── Sheet 1: Option Chain ──
        chain_out = chain_df[[c for c in
            ["strike_price","ce_oi","ce_chng_oi","ce_volume","ce_ltp","CE Bias",
             "Strike Signal","Big Move","pe_ltp","pe_volume","pe_chng_oi","pe_oi","PE Bias",
             "ce_iv","pe_iv"] if c in chain_df.columns]].copy()
        chain_out.rename(columns={
            "strike_price":"Strike","ce_oi":"CE OI","ce_chng_oi":"CE ΔOI","ce_volume":"CE Vol",
            "ce_ltp":"CE LTP","pe_ltp":"PE LTP","pe_volume":"PE Vol","pe_chng_oi":"PE ΔOI",
            "pe_oi":"PE OI","ce_iv":"CE IV%","pe_iv":"PE IV%"}, inplace=True)
        chain_out.to_excel(writer, index=False, sheet_name="Option Chain")

        ws = writer.sheets["Option Chain"]
        hdr_font  = Font(bold=True, color="FFFFFF", name="Calibri", size=10)
        hdr_fill  = PatternFill("solid", start_color="1F2937")
        thin      = Side(style="thin", color="30363D")
        border    = Border(left=thin, right=thin, top=thin, bottom=thin)

        for cell in ws[1]:
            cell.font      = hdr_font
            cell.fill      = hdr_fill
            cell.alignment = Alignment(horizontal="center", vertical="center")
            cell.border    = border

        green_fill  = PatternFill("solid", start_color="0D3B2E")
        red_fill    = PatternFill("solid", start_color="3B0D1A")
        yellow_fill = PatternFill("solid", start_color="1C2128")

        strike_col = None
        for idx, cell in enumerate(ws[1], 1):
            if cell.value == "Strike": strike_col = idx

        for row in ws.iter_rows(min_row=2):
            for cell in row:
                cell.border    = border
                cell.alignment = Alignment(horizontal="center")
                val = str(cell.value or "")
                if "BUY" in val or "Put Writing" in val or "Unwinding" in val and "PE" not in val:
                    cell.fill = green_fill; cell.font = Font(color="3FB950", bold=True, name="Calibri")
                elif "SELL" in val or "Call Writing" in val:
                    cell.fill = red_fill;   cell.font = Font(color="F85149", bold=True, name="Calibri")
                elif "Big Move" in val:
                    cell.fill = yellow_fill; cell.font = Font(color="D29922", bold=True, name="Calibri")

        for col_cells in ws.columns:
            width = max((len(str(c.value)) for c in col_cells if c.value), default=10)
            ws.column_dimensions[col_cells[0].column_letter].width = max(width+3, 12)
        ws.freeze_panes = "A2"
        ws.row_dimensions[1].height = 20

        # ── Sheet 2: Big Move Scores ──
        if not scored_df.empty:
            out2 = scored_df[["Strike","Big Move Score","Direction","Reason"]].copy()
            out2["Probability"] = out2["Big Move Score"].apply(score_tier)
            out2["Strike"] = out2["Strike"].apply(lambda x: f"{x:,.0f}")
            out2.to_excel(writer, index=False, sheet_name="Big Move Analysis")

            ws2 = writer.sheets["Big Move Analysis"]
            for cell in ws2[1]:
                cell.font = hdr_font; cell.fill = hdr_fill
                cell.alignment = Alignment(horizontal="center"); cell.border = border

            fire_fill = PatternFill("solid", start_color="3B0D1A")
            high_fill = PatternFill("solid", start_color="0D3B2E")
            med_fill  = PatternFill("solid", start_color="1C2128")

            for row in ws2.iter_rows(min_row=2):
                try:   score = float(row[1].value)
                except: score = 0
                fill = fire_fill if score>=80 else (high_fill if score>=60 else (med_fill if score>=40 else None))
                col  = ("F85149" if score>=80 else ("3FB950" if score>=60 else ("D29922" if score>=40 else "8B949E")))
                for cell in row:
                    cell.border = border; cell.alignment = Alignment(horizontal="center")
                    if fill: cell.fill = fill
                    cell.font = Font(color=col, bold=(score>=60), name="Calibri")

            for col_cells in ws2.columns:
                width = max((len(str(c.value)) for c in col_cells if c.value), default=10)
                ws2.column_dimensions[col_cells[0].column_letter].width = max(width+3, 14)
            ws2.freeze_panes = "A2"

        # ── Sheet 3: Summary ──
        summary = pd.DataFrame([
            {"Metric":"Spot Price",        "Value": f"₹{spot:,.2f}" if spot else "—"},
            {"Metric":"PCR",               "Value": f"{pcr:.3f}"},
            {"Metric":"PCR Signal",        "Value": "Bullish" if pcr>1.3 else ("Bearish" if pcr<0.7 else "Neutral")},
            {"Metric":"Max Pain",          "Value": f"₹{max_pain:,.0f}"},
            {"Metric":"Spot vs Max Pain",  "Value": f"{'Above' if spot>max_pain else 'Below'} by {abs(spot-max_pain):,.0f}"},
            {"Metric":"Scan Time",         "Value": datetime.now().strftime("%d-%b-%Y %I:%M %p")},
        ])
        summary.to_excel(writer, index=False, sheet_name="Summary")
        ws3 = writer.sheets["Summary"]
        for cell in ws3[1]:
            cell.font = hdr_font; cell.fill = hdr_fill; cell.alignment = Alignment(horizontal="center")
        for col_cells in ws3.columns:
            width = max((len(str(c.value)) for c in col_cells if c.value), default=10)
            ws3.column_dimensions[col_cells[0].column_letter].width = max(width+4, 20)

    buf.seek(0)
    return buf.getvalue()


# ─── Chain Table Styling ────────────────────────────────────────────────────
def style_chain_table(df):
    cols = ["ce_oi","ce_chng_oi","ce_volume","ce_ltp","CE Bias","strike_price",
            "PE Bias","pe_ltp","pe_volume","pe_chng_oi","pe_oi","Strike Signal","Big Move"]
    out = df[[c for c in cols if c in df.columns]].copy()
    out.rename(columns={"ce_oi":"CE OI","ce_chng_oi":"CE ΔOI","ce_volume":"CE Vol","ce_ltp":"CE LTP",
                         "strike_price":"Strike ⚡","pe_ltp":"PE LTP","pe_volume":"PE Vol",
                         "pe_chng_oi":"PE ΔOI","pe_oi":"PE OI"}, inplace=True)
    return out


def color_chain_row(row):
    sig = str(row.get("Strike Signal",""))
    bm  = str(row.get("Big Move",""))
    if "BUY"  in sig: return ["background-color:#0d3b2e; color:#3fb950"]*len(row)
    if "SELL" in sig: return ["background-color:#3b0d1a; color:#f85149"]*len(row)
    if "Big Move" in bm: return ["background-color:#1c2128; color:#d29922"]*len(row)
    return [""]*len(row)


# ─── Main ────────────────────────────────────────────────────────────────────
def show_option_chain(fyers):
    st.markdown("## 📊 Master Options Chain Dashboard")
    st.markdown("<hr style='border-color:#30363d;margin:0 0 20px 0'>", unsafe_allow_html=True)

    with st.sidebar:
        st.markdown("### ⚙️ Configuration")
        option_type = st.radio("Instrument Type", ["Indices", "F&O Stocks"])
        is_stock = option_type == "F&O Stocks"

        if not is_stock:
            selected_key = st.selectbox("Index", list(SYMBOL_MAP.keys()))
            symbol = SYMBOL_MAP[selected_key]
        else:
            stock  = st.text_input("Stock Symbol (e.g. RELIANCE)", "RELIANCE")
            symbol = normalize_symbol(stock)

        max_strikes  = 20 if is_stock else 30
        strike_count = st.slider("Strikes Around ATM", 5, max_strikes, min(20, max_strikes), step=5)

        expiry_options = st.session_state.get("oc_expiry_list", [])
        if expiry_options:
            expiry_labels        = [l for l,_ in expiry_options]
            selected_expiry_label = st.selectbox("Expiry", expiry_labels)
            expiry_timestamp     = dict(expiry_options).get(selected_expiry_label, "")
        else:
            st.caption("Expiry list loads after first fetch.")
            expiry_timestamp = ""; selected_expiry_label = ""

        debug_mode = st.checkbox("Show raw API response (debug)", value=False)
        st.divider()
        fetch_btn = st.button("🔄 Fetch Live Data", use_container_width=True, type="primary")

    # ── Fetch ────────────────────────────────────────────────────────────────
    if fetch_btn:
        with st.spinner("Connecting to Fyers API …"):
            response, used_symbol, attempts = fetch_optionchain_with_fallback(
                fyers, symbol, strike_count, is_stock, expiry_timestamp)

        if debug_mode:
            st.write("**Symbols tried:**", attempts); st.json(response)

        if not response:
            st.error("API call failed. Check Fyers connection/token."); return

        if response.get("s") != "ok":
            st.error(f"API Error (code {response.get('code','—')}): {response.get('message','No data')}\n\n"
                     f"Tried: {', '.join(s for s,_ in attempts)}"); return

        symbol = used_symbol
        new_exp = extract_expiry_list(response)
        if new_exp: st.session_state["oc_expiry_list"] = new_exp

        options_data, data = extract_options_data(response)
        spot_price = extract_spot_price(response, data)

        if not spot_price:
            try:
                q = fyers.quotes(data={"symbols": symbol})
                spot_price = float((q.get("d",[{}])[0].get("v",{}) if isinstance(q,dict) else {}).get("lp",0) or 0)
            except Exception: pass

        if not options_data:
            st.warning("⚠️ No options data returned. Market may be closed, symbol invalid, or "
                       "API response format changed. Enable debug mode and re-fetch."); return

        df = normalize_chain_shape(options_data)
        num_cols = ["strike_price","ce_ltp","ce_oi","ce_volume","ce_chng_oi",
                    "pe_ltp","pe_oi","pe_volume","pe_chng_oi"]
        for col in num_cols:
            df[col] = pd.to_numeric(df.get(col, 0), errors="coerce").fillna(0)
        df.sort_values("strike_price", inplace=True)
        df.reset_index(drop=True, inplace=True)
        df = compute_strike_bias(df)
        df = add_iv_columns(df, spot_price, selected_expiry_label)

        st.session_state["oc_df"]     = df
        st.session_state["oc_spot"]   = spot_price
        st.session_state["oc_symbol"] = symbol

    if "oc_df" not in st.session_state:
        st.info("👈 Choose an instrument and click **Fetch Live Data**."); return

    df         = st.session_state["oc_df"]
    spot_price = st.session_state["oc_spot"]
    total_ce   = df["ce_oi"].sum(); total_pe = df["pe_oi"].sum()
    pcr        = total_pe/total_ce if total_ce>0 else 0
    max_pain   = calculate_max_pain(df)
    atm_strike = (df.iloc[(df["strike_price"]-spot_price).abs().argsort().iloc[:1]]["strike_price"].values[0]
                  if spot_price else df["strike_price"].median())

    # ── KPI row ──────────────────────────────────────────────────────────────
    c1,c2,c3,c4,c5 = st.columns(5)
    c1.metric("Spot Price",  f"₹{spot_price:,.2f}" if spot_price else "—")
    c2.metric("ATM Strike",  f"₹{atm_strike:,.0f}")
    c3.metric("Total CE OI", f"{total_ce/1e5:.1f}L")
    c4.metric("Total PE OI", f"{total_pe/1e5:.1f}L")
    c5.metric("Max Pain",    f"₹{max_pain:,.0f}")
    st.markdown("<br>", unsafe_allow_html=True)

    # ── Quick alerts ──────────────────────────────────────────────────────────
    for alert in detect_big_moves(df)[:4]:
        fn = st.success if alert["direction"]=="BUY" else st.error
        fn(f"{'🟢 BUY' if alert['direction']=='BUY' else '🔴 SELL'} · "
           f"Strike **{alert['strike']:,.0f}** ({alert['side']}) · "
           f"ΔOI {alert['oi_change']:+,.0f} — {alert['note']}")

    sig_col, gauge_col = st.columns([1,1])
    with sig_col:
        st.markdown("**Market Sentiment**")
        st.markdown(pcr_signal(pcr), unsafe_allow_html=True)
        st.markdown(f"<br>PCR = **{pcr:.3f}**  |  Max Pain = **{max_pain:,.0f}**", unsafe_allow_html=True)
        st.markdown(f"🛡️ Support (max PE OI): **{df.loc[df['pe_oi'].idxmax(),'strike_price']:,.0f}**")
        st.markdown(f"🧱 Resistance (max CE OI): **{df.loc[df['ce_oi'].idxmax(),'strike_price']:,.0f}**")
        st.markdown(f"📏 Spot vs Max Pain: **{'Above' if spot_price>max_pain else 'Below'} by {abs(spot_price-max_pain):,.0f}**")
    with gauge_col:
        st.plotly_chart(pcr_gauge(pcr), use_container_width=True, config={"displayModeBar":False})

    st.divider()

    # ── Tabs ──────────────────────────────────────────────────────────────────
    tab1, tab2, tab3, tab4 = st.tabs(["📋 Chain Table","📊 OI Analysis","📈 IV Skew","🔥 Big Move Analysis"])

    with tab1:
        bm_df = df[df.get("Big Move","")=="🚨 Big Move"] if "Big Move" in df.columns else pd.DataFrame()
        if not bm_df.empty:
            buys  = bm_df[bm_df["Strike Signal"]=="🟢 BUY"]["strike_price"].tolist()
            sells = bm_df[bm_df["Strike Signal"]=="🔴 SELL"]["strike_price"].tolist()
            parts = []
            if buys:  parts.append(f"🟢 **Buy build-up:** {', '.join(f'{s:,.0f}' for s in buys)}")
            if sells: parts.append(f"🔴 **Sell build-up:** {', '.join(f'{s:,.0f}' for s in sells)}")
            if parts: st.markdown("🚨 **Big OI moves** — " + "  |  ".join(parts))

        display_df = style_chain_table(df)
        num_cols_d = [c for c in display_df.select_dtypes("number").columns]
        st.dataframe(
            display_df.style
                .apply(color_chain_row, axis=1)
                .background_gradient(subset=[c for c in ["CE OI","PE OI"] if c in display_df.columns],
                                     cmap="RdYlGn", vmin=0)
                .format({c:"{:,.0f}" for c in num_cols_d}),
            use_container_width=True, height=540)

    with tab2:
        st.markdown("##### Open Interest — Calls vs Puts")
        st.plotly_chart(oi_bar_chart(df, max_pain), use_container_width=True, config={"displayModeBar":False})
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**Top 5 CE OI Strikes**")
            st.dataframe(df.nlargest(5,"ce_oi")[["strike_price","ce_oi","ce_ltp"]].reset_index(drop=True)
                           .style.format({"ce_oi":"{:,.0f}","ce_ltp":"{:.2f}"}),
                         use_container_width=True, height=215)
        with c2:
            st.markdown("**Top 5 PE OI Strikes**")
            st.dataframe(df.nlargest(5,"pe_oi")[["strike_price","pe_oi","pe_ltp"]].reset_index(drop=True)
                           .style.format({"pe_oi":"{:,.0f}","pe_ltp":"{:.2f}"}),
                         use_container_width=True, height=215)

    with tab3:
        st.markdown("##### Implied Volatility Skew")
        st.plotly_chart(iv_chart(df), use_container_width=True, config={"displayModeBar":False})

    with tab4:
        st.markdown("##### 🔥 Big Move Ready Strike Analysis — 7-Point Scoring")
        st.caption("Score (0–100) across: ΔOI surge · OI level · Volume · OI+Vol confirmation · "
                   "Spot proximity · PCR alignment · Max Pain. Not financial advice.")

        scored = compute_big_move_scores(df, spot_price, pcr, max_pain)

        if scored.empty:
            st.info("No data — fetch the chain first.")
        else:
            scored["Probability"] = scored["Big Move Score"].apply(score_tier)
            st.plotly_chart(big_move_score_chart(scored), use_container_width=True, config={"displayModeBar":False})

            show = scored[["Strike","Big Move Score","Probability","Direction","Reason"]].copy()
            show["Strike"] = show["Strike"].apply(lambda x: f"{x:,.0f}")

            def bm_row_color(row):
                s = scored.loc[row.name,"Big Move Score"]
                if s>=80: bg,fg = "#3b0d1a","#ff6b35"
                elif s>=60: bg,fg = "#0d3b2e","#3fb950"
                elif s>=40: bg,fg = "#1c2128","#d29922"
                else:       bg,fg = "#161b22","#8b949e"
                return [f"background-color:{bg};color:{fg}"]*len(row)

            st.dataframe(show.style.apply(bm_row_color, axis=1), use_container_width=True, height=500)

            st.markdown("---")
            st.markdown("##### ✅ Final Recommendation")
            rec = build_recommendations(scored, spot_price, max_pain)

            r1, r2 = st.columns(2)
            with r1:
                b = rec.get("best_buy")
                st.success(f"**✅ Best BUY Strike:** {b['Strike']:,.0f}  "
                           f"(Score {b['Big Move Score']:.0f}) — {b['Reason']}" if b else "No qualifying BUY strike")
                bo = rec.get("breakout")
                st.info(f"**📈 Breakout Strike (above spot):** {bo['Strike']:,.0f}  "
                        f"(Score {bo['Big Move Score']:.0f})" if bo else "N/A")
            with r2:
                s = rec.get("best_sell")
                st.error(f"**✅ Best SELL Strike:** {s['Strike']:,.0f}  "
                         f"(Score {s['Big Move Score']:.0f}) — {s['Reason']}" if s else "No qualifying SELL strike")
                bd = rec.get("breakdown")
                st.warning(f"**📉 Breakdown Strike (below spot):** {bd['Strike']:,.0f}  "
                           f"(Score {bd['Big Move Score']:.0f})" if bd else "N/A")

            avoid = rec.get("avoid",[])
            if avoid:
                st.caption(f"⚪ **Avoid (score < 40):** {', '.join(f'{s:,.0f}' for s in avoid[:10])}")

            st.caption("Confirm with price action before trading. This is a positioning read only.")

        # ── Excel Download ── (available regardless of which tab is active)
        st.divider()
        st.markdown("##### 📥 Download Full Report")
        if not scored.empty:
            xlsx = to_excel_bytes(df, scored, spot_price, pcr, max_pain)
            st.download_button(
                "📥 Download Excel Report (Chain + Big Move + Summary)",
                data=xlsx,
                file_name=f"options_chain_{st.session_state.get('oc_symbol','').replace(':','-')}_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
