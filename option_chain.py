import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import numpy as np

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
    cols = ["ce_oi", "ce_chng_oi", "ce_volume", "ce_ltp", "strike_price", "pe_ltp", "pe_volume", "pe_chng_oi", "pe_oi"]
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


def normalize_symbol(stock: str) -> str:
    """Fyers' optionchain endpoint expects the underlying symbol, not the
    equity (-EQ) symbol used for quotes/LTP. Strip -EQ if present."""
    stock = stock.strip().upper()
    if stock.endswith("-EQ"):
        stock = stock[:-3]
    return f"NSE:{stock}"


# ─── Main Function ───────────────────────────────────────────────────────────
def show_option_chain(fyers):
    st.markdown("## 📊 Master Options Chain Dashboard")
    st.markdown("<hr style='border-color:#30363d;margin:0 0 20px 0'>", unsafe_allow_html=True)

    # ── Sidebar ──────────────────────────────────────────────────────────────
    with st.sidebar:
        st.markdown("### ⚙️ Configuration")
        symbol_map = {
            "NIFTY 50":     "NSE:NIFTY50-INDEX",
            "NIFTY BANK":   "NSE:NIFTYBANK-INDEX",
            "SENSEX":       "BSE:SENSEX-INDEX",
            "BANKEX":       "BSE:BANKEX-INDEX",
            "NIFTY NXT 50": "NSE:NIFTYNEXT50-INDEX",
        }
        option_type = st.radio("Instrument Type", ["Indices", "F&O Stocks"])
        if option_type == "Indices":
            selected_key = st.selectbox("Index", list(symbol_map.keys()))
            symbol = symbol_map[selected_key]
        else:
            stock = st.text_input("Stock Symbol (e.g. RELIANCE)", "RELIANCE")
            symbol = normalize_symbol(stock)

        strike_count = st.slider("Strikes Around ATM", 5, 30, 20, step=5)
        debug_mode = st.checkbox("Show raw API response (debug)", value=False)
        st.divider()
        fetch_btn = st.button("🔄 Fetch Live Data", use_container_width=True, type="primary")

    # ── Fetch & Process ──────────────────────────────────────────────────────
    if fetch_btn:
        with st.spinner("Connecting to Fyers API …"):
            try:
                response = fyers.optionchain(data={"symbol": symbol, "strikecount": int(strike_count)})
            except Exception as e:
                st.error(f"API call failed: {e}")
                return

        if debug_mode:
            st.json(response)

        if not response or response.get("s") != "ok":
            st.error(f"API Error: {response.get('message', 'No data returned')}")
            return

        options_data, data = extract_options_data(response)
        spot_price = data.get("ltp", 0) if isinstance(data, dict) else 0

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
