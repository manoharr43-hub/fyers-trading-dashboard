"""
option_chain.py
NSE/F&O + MCX Commodity Option Chain for the Streamlit Fyers app.

IMPORTANT MCX FIX:
Do NOT send MCX:CRUDEOIL / MCX:GOLD directly to the Fyers option-chain API.
MCX option chains are resolved from the current MCX futures contract in the
FYERS MCX_COM symbol master, then that exact FUT symbol is sent as the
underlying to /data/options-chain-v3.

app.py already calls:
    from option_chain import show_option_chain
    show_option_chain(fyers)
"""

import io
from datetime import datetime, timezone
from typing import Optional, Tuple, Dict, Any

import numpy as np
import pandas as pd
import requests
import streamlit as st


MCX_MASTER_URL = "https://public.fyers.in/sym_details/MCX_COM.csv"
OPTION_CHAIN_URL = "https://api-t1.fyers.in/data/options-chain-v3"

MCX_ROOTS = [
    "GOLD",
    "SILVER",
    "CRUDEOIL",
    "NATURALGAS",
    "COPPER",
    "ZINC",
    "ALUMINIUM",
    "LEAD",
    "NICKEL",
    "SILVERM",
    "GOLDM",
]

# ============================================================
# COMMON HELPERS
# ============================================================

def _ist_now():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Kolkata"))
    except Exception:
        return datetime.now(timezone.utc)


def _num(x, default=np.nan):
    try:
        if x is None or x == "":
            return default
        return float(x)
    except Exception:
        return default


def _excel(df: pd.DataFrame, sheet="OPTION_CHAIN"):
    b = io.BytesIO()
    with pd.ExcelWriter(b, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name=sheet[:31])
    return b.getvalue()


# ============================================================
# MCX SYMBOL MASTER
# ============================================================

@st.cache_data(ttl=300, show_spinner=False)
def load_mcx_master() -> pd.DataFrame:
    """
    FYERS publishes the current MCX symbol master as MCX_COM.csv.
    The file has no header. Important columns used here are:
      1  = contract description
      2  = instrument type
      8  = expiry epoch
      9  = FYERS symbol
      13 = underlying/root
      15 = strike
      16 = option type
    """
    r = requests.get(MCX_MASTER_URL, timeout=20)
    r.raise_for_status()

    # Keep all columns because the FYERS file is positional.
    df = pd.read_csv(
        io.StringIO(r.text),
        header=None,
        dtype=str,
        on_bad_lines="skip",
    )

    if df.shape[1] < 17:
        raise RuntimeError(
            f"Unexpected MCX symbol master format: {df.shape[1]} columns"
        )

    df["expiry_epoch"] = pd.to_numeric(df.iloc[:, 8], errors="coerce")
    df["fy_symbol"] = df.iloc[:, 9].astype(str).str.strip()
    df["root"] = df.iloc[:, 13].astype(str).str.strip().str.upper()
    df["instrument_type"] = pd.to_numeric(
        df.iloc[:, 2], errors="coerce"
    )
    df["strike"] = pd.to_numeric(
        df.iloc[:, 15], errors="coerce"
    )
    df["option_type"] = df.iloc[:, 16].astype(str).str.upper().str.strip()
    df["description"] = df.iloc[:, 1].astype(str).str.strip()

    return df


def get_mcx_futures(master: pd.DataFrame, root: str) -> pd.DataFrame:
    """Return non-expired MCX FUT contracts for one commodity root."""
    root = root.upper().strip()
    now_epoch = int(_ist_now().timestamp())

    d = master[
        (master["root"] == root)
        & (master["instrument_type"] == 11)
        & (master["option_type"] == "XX")
        & master["fy_symbol"].str.startswith("MCX:", na=False)
        & (master["expiry_epoch"] > now_epoch)
    ].copy()

    return d.sort_values("expiry_epoch").reset_index(drop=True)


def get_current_mcx_future(
    master: pd.DataFrame, root: str
) -> Tuple[Optional[str], Optional[int], pd.DataFrame]:
    d = get_mcx_futures(master, root)
    if d.empty:
        return None, None, d

    row = d.iloc[0]
    return (
        str(row["fy_symbol"]),
        int(row["expiry_epoch"]),
        d,
    )


# ============================================================
# FYERS OPTION CHAIN
# ============================================================

def _fyers_option_chain(
    fyers,
    symbol: str,
    strikecount: int = 10,
    timestamp: Optional[int] = None,
):
    """
    Prefer the official FYERS SDK method. Fall back to direct v3 HTTP.
    """
    payload = {
        "symbol": symbol,
        "strikecount": min(max(int(strikecount), 1), 50),
    }

    if timestamp:
        payload["timestamp"] = str(int(timestamp))

    # SDK path
    fn = getattr(fyers, "option_chain", None)
    if callable(fn):
        try:
            return fn(data=payload)
        except TypeError:
            try:
                return fn(payload)
            except Exception:
                pass
        except Exception:
            pass

    # Direct REST fallback
    client_id = getattr(fyers, "client_id", None)
    token = getattr(fyers, "token", None)

    if not client_id:
        try:
            client_id = st.secrets["FYERS_CLIENT_ID"]
        except Exception:
            client_id = None

    if not token:
        token = st.session_state.get("access_token")

    if not client_id or not token:
        raise RuntimeError(
            "FYERS client/token is not available in this Streamlit session."
        )

    headers = {
        "Authorization": f"{client_id}:{token}",
        "Content-Type": "application/json",
    }

    r = requests.get(
        OPTION_CHAIN_URL,
        headers=headers,
        params=payload,
        timeout=15,
    )

    try:
        result = r.json()
    except Exception:
        result = {
            "s": "error",
            "code": r.status_code,
            "message": r.text[:500],
        }

    return result


# ============================================================
# PARSE OPTION CHAIN
# ============================================================

def parse_chain(resp: dict):
    if not isinstance(resp, dict):
        return pd.DataFrame(), {}

    data = resp.get("data") or {}
    rows = data.get("optionsChain") or []
    expiry_data = data.get("expiryData") or []

    spot = np.nan

    # Underlying entry has option_type == "" and strike == -1.
    for x in rows:
        if x.get("option_type", "") == "":
            spot = _num(x.get("ltp"))
            break

    parsed = []

    for x in rows:
        typ = str(x.get("option_type", "")).upper()
        if typ not in ("CE", "PE"):
            continue

        g = x.get("greeks") or {}

        parsed.append({
            "Strike": _num(x.get("strike_price")),
            "Type": typ,
            "Symbol": x.get("symbol", ""),
            "LTP": _num(x.get("ltp")),
            "Change": _num(x.get("ltpch")),
            "Change %": _num(x.get("ltpchp")),
            "Bid": _num(x.get("bid")),
            "Ask": _num(x.get("ask")),
            "OI": _num(x.get("oi"), 0),
            "OI Change": _num(x.get("oich"), 0),
            "OI Change %": _num(x.get("oichp")),
            "Prev OI": _num(x.get("prev_oi"), 0),
            "Volume": _num(x.get("volume"), 0),
            "IV": _num(g.get("iv", x.get("iv"))),
            "Delta": _num(g.get("delta", x.get("delta"))),
            "Gamma": _num(g.get("gamma", x.get("gamma"))),
            "Theta": _num(g.get("theta", x.get("theta"))),
            "Vega": _num(g.get("vega", x.get("vega"))),
        })

    df = pd.DataFrame(parsed)

    meta = {
        "spot": spot,
        "expiry_data": expiry_data,
        "call_oi": _num(data.get("callOi"), 0),
        "put_oi": _num(data.get("putOi"), 0),
    }

    if not df.empty and np.isfinite(spot):
        strikes = sorted(df["Strike"].dropna().unique())
        if strikes:
            meta["atm"] = min(strikes, key=lambda s: abs(s - spot))

    return df, meta


# ============================================================
# ANALYTICS
# ============================================================

def calculate_max_pain(df: pd.DataFrame):
    if df.empty:
        return np.nan

    strikes = sorted(df["Strike"].dropna().unique())
    if not strikes:
        return np.nan

    ce = df[df["Type"] == "CE"].groupby("Strike")["OI"].sum().to_dict()
    pe = df[df["Type"] == "PE"].groupby("Strike")["OI"].sum().to_dict()

    pains = {}

    for settle in strikes:
        call_pain = sum(
            max(settle - k, 0) * oi
            for k, oi in ce.items()
        )
        put_pain = sum(
            max(k - settle, 0) * oi
            for k, oi in pe.items()
        )
        pains[settle] = call_pain + put_pain

    return min(pains, key=pains.get) if pains else np.nan


def add_pressure(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    out = df.copy()
    out["Pressure"] = "NEUTRAL"

    for i, r in out.iterrows():
        oi_ch = _num(r["OI Change"], 0)
        vol = _num(r["Volume"], 0)
        ltp_ch = _num(r["Change %"])

        if r["Type"] == "CE":
            if oi_ch > 0 and np.isfinite(ltp_ch) and ltp_ch < 0:
                out.at[i, "Pressure"] = "CALL WRITING"
            elif oi_ch > 0 and np.isfinite(ltp_ch) and ltp_ch > 0:
                out.at[i, "Pressure"] = "CALL BUYING"
        else:
            if oi_ch > 0 and np.isfinite(ltp_ch) and ltp_ch < 0:
                out.at[i, "Pressure"] = "PUT WRITING"
            elif oi_ch > 0 and np.isfinite(ltp_ch) and ltp_ch > 0:
                out.at[i, "Pressure"] = "PUT BUYING"

        if vol > 0 and oi_ch > 0:
            # Keep the label above; this branch simply preserves valid rows.
            pass

    return out


# ============================================================
# MCX UI
# ============================================================

def show_mcx(fyers):
    st.subheader("🪙 MCX Commodity Option Chain")

    try:
        master = load_mcx_master()
    except Exception as e:
        st.error(f"Could not load FYERS MCX symbol master: {e}")
        st.info(
            "FYERS publishes the MCX_COM symbol master; the app needs internet "
            "access to read the current contracts."
        )
        return

    root = st.selectbox(
        "MCX Commodity",
        MCX_ROOTS,
        index=0,
        key="mcx_oc_root",
    )

    current_symbol, current_expiry, futures = get_current_mcx_future(
        master, root
    )

    if futures.empty:
        st.error(
            f"No current MCX FUT contract found for {root} in the FYERS symbol master."
        )
        return

    # Contract selector — default nearest valid future.
    options = []
    for _, r in futures.head(6).iterrows():
        label = (
            f"{r['description']}  |  {r['fy_symbol']}"
        )
        options.append((label, r["fy_symbol"], int(r["expiry_epoch"])))

    labels = [x[0] for x in options]

    selected_label = st.selectbox(
        "Underlying FUT Contract",
        labels,
        index=0,
        key="mcx_oc_future",
    )

    selected = options[labels.index(selected_label)]
    underlying = selected[1]
    future_expiry = selected[2]

    st.success(
        f"Using exact FYERS MCX underlying: `{underlying}`"
    )

    st.caption(
        "This is the important fix: the option-chain request uses the "
        "current MCX FUT symbol from MCX_COM.csv, not `MCX:{root}`."
    )

    # Let the user choose expiry returned by the option-chain API.
    strikecount = st.slider(
        "ATM ± strikes",
        1,
        50,
        10,
        key="mcx_oc_strikecount",
    )

    if st.button(
        "📡 FETCH MCX LIVE OPTION CHAIN",
        type="primary",
        use_container_width=True,
        key="mcx_oc_fetch",
    ):
        with st.spinner(f"Fetching {root} option chain..."):
            try:
                # First request without timestamp gets expiryData.
                first = _fyers_option_chain(
                    fyers,
                    underlying,
                    strikecount=1,
                )

                if not isinstance(first, dict):
                    st.error("Invalid FYERS option-chain response.")
                    return

                if first.get("s") != "ok":
                    st.error(
                        f"FYERS option-chain error: "
                        f"{first.get('message', first)}"
                    )
                    return

                expiry_data = (
                    first.get("data", {}).get("expiryData") or []
                )

                if not expiry_data:
                    st.error(
                        f"FYERS returned no option expiries for `{underlying}`."
                    )
                    st.json(first)
                    return

                # Use nearest returned expiry by default.
                expiry_choices = []
                for e in expiry_data:
                    expiry_choices.append({
                        "label": str(e.get("date", e.get("expiry"))),
                        "timestamp": int(e["expiry"]),
                        "flag": e.get("expiry_flag", ""),
                    })

                # Store choices in session state so they survive reruns.
                st.session_state["mcx_expiry_choices"] = expiry_choices

                # Fetch nearest expiry immediately.
                expiry_ts = expiry_choices[0]["timestamp"]

                full = _fyers_option_chain(
                    fyers,
                    underlying,
                    strikecount=strikecount,
                    timestamp=expiry_ts,
                )

                if not isinstance(full, dict):
                    st.error("Invalid full MCX option-chain response.")
                    return

                if full.get("s") != "ok":
                    st.error(
                        f"FYERS MCX option-chain error: "
                        f"{full.get('message', full)}"
                    )
                    return

                st.session_state["mcx_chain_response"] = full
                st.session_state["mcx_chain_symbol"] = underlying
                st.session_state["mcx_chain_root"] = root
                st.session_state["mcx_chain_expiry_choices"] = expiry_choices

            except Exception as e:
                st.error(f"MCX option-chain request failed: {e}")

    # --------------------------------------------------------
    # Expiry selector after first successful request
    # --------------------------------------------------------
    choices = st.session_state.get(
        "mcx_chain_expiry_choices",
        [],
    )

    if choices:
        labels = [
            f"{x['label']} ({x['flag']})"
            for x in choices
        ]

        expiry_label = st.selectbox(
            "Option Expiry",
            labels,
            key="mcx_selected_expiry",
        )

        idx = labels.index(expiry_label)
        expiry_ts = choices[idx]["timestamp"]

        if st.button(
            "🔄 FETCH SELECTED EXPIRY",
            use_container_width=True,
            key="mcx_fetch_selected_expiry",
        ):
            try:
                resp = _fyers_option_chain(
                    fyers,
                    st.session_state["mcx_chain_symbol"],
                    strikecount=strikecount,
                    timestamp=expiry_ts,
                )

                if resp.get("s") != "ok":
                    st.error(
                        f"FYERS error: {resp.get('message', resp)}"
                    )
                else:
                    st.session_state["mcx_chain_response"] = resp
                    st.rerun()

            except Exception as e:
                st.error(f"Selected expiry fetch failed: {e}")

    # --------------------------------------------------------
    # Display
    # --------------------------------------------------------
    resp = st.session_state.get("mcx_chain_response")

    if not resp:
        st.info(
            "Choose an MCX commodity and click "
            "'FETCH MCX LIVE OPTION CHAIN'."
        )
        return

    df, meta = parse_chain(resp)

    if df.empty:
        st.warning(
            "FYERS returned no CE/PE rows for this MCX contract."
        )
        return

    df = add_pressure(df)

    spot = meta.get("spot")
    atm = meta.get("atm")
    call_oi = meta.get("call_oi", 0)
    put_oi = meta.get("put_oi", 0)

    pcr = (
        put_oi / call_oi
        if call_oi and np.isfinite(call_oi)
        else np.nan
    )

    max_pain = calculate_max_pain(df)

    c1, c2, c3, c4, c5 = st.columns(5)

    c1.metric(
        "Underlying LTP",
        f"{spot:.2f}" if np.isfinite(spot) else "N/A",
    )
    c2.metric(
        "ATM",
        f"{atm:.2f}" if atm is not None else "N/A",
    )
    c3.metric("CE OI", f"{call_oi:,.0f}")
    c4.metric("PE OI", f"{put_oi:,.0f}")
    c5.metric(
        "PCR",
        f"{pcr:.2f}" if np.isfinite(pcr) else "N/A",
    )

    c6, c7 = st.columns(2)
    c6.metric(
        "Max Pain",
        f"{max_pain:.2f}" if np.isfinite(max_pain) else "N/A",
    )
    c7.write(
        f"**Exact FYERS Symbol:** `{st.session_state.get('mcx_chain_symbol')}`"
    )

    # ATM-centered display.
    if atm is not None:
        strikes = sorted(df["Strike"].dropna().unique())
        if strikes:
            pos = min(
                range(len(strikes)),
                key=lambda i: abs(strikes[i] - atm),
            )
            lo = max(0, pos - strikecount)
            hi = min(len(strikes), pos + strikecount + 1)
            selected_strikes = strikes[lo:hi]
            display_df = df[
                df["Strike"].isin(selected_strikes)
            ].copy()
        else:
            display_df = df.copy()
    else:
        display_df = df.copy()

    display_df = display_df.sort_values(
        ["Strike", "Type"]
    ).reset_index(drop=True)

    st.subheader("📋 MCX CE / PE Chain")
    st.dataframe(
        display_df,
        use_container_width=True,
        height=550,
    )

    st.download_button(
        "📥 DOWNLOAD MCX OPTION CHAIN EXCEL",
        _excel(display_df, "MCX_OPTION_CHAIN"),
        f"MCX_OPTION_CHAIN_{_ist_now().strftime('%Y%m%d_%H%M')}.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key="mcx_option_excel",
    )


# ============================================================
# NSE / F&O BASIC UI
# ============================================================

def show_nse_fno(fyers):
    st.subheader("🇮🇳 NSE / F&O Option Chain")

    symbol = st.text_input(
        "Underlying FYERS symbol",
        value="NSE:NIFTY50-INDEX",
        help=(
            "Examples: NSE:NIFTY50-INDEX, NSE:NIFTYBANK-INDEX, "
            "NSE:SBIN-EQ. Use the exact FYERS underlying symbol."
        ),
        key="nse_oc_symbol",
    ).strip().upper()

    strikecount = st.slider(
        "ATM ± strikes",
        1,
        50,
        10,
        key="nse_oc_strikecount",
    )

    if st.button(
        "📡 FETCH NSE/F&O LIVE OPTION CHAIN",
        type="primary",
        use_container_width=True,
        key="nse_oc_fetch",
    ):
        try:
            resp = _fyers_option_chain(
                fyers,
                symbol,
                strikecount=strikecount,
            )

            if resp.get("s") != "ok":
                st.error(
                    f"FYERS error: {resp.get('message', resp)}"
                )
                return

            st.session_state["nse_oc_resp"] = resp

        except Exception as e:
            st.error(f"Option-chain request failed: {e}")

    resp = st.session_state.get("nse_oc_resp")
    if not resp:
        st.info("Enter a valid FYERS underlying and fetch live data.")
        return

    df, meta = parse_chain(resp)

    if df.empty:
        st.warning("No CE/PE rows returned.")
        return

    df = add_pressure(df)

    spot = meta.get("spot")
    call_oi = meta.get("call_oi", 0)
    put_oi = meta.get("put_oi", 0)
    pcr = put_oi / call_oi if call_oi else np.nan
    max_pain = calculate_max_pain(df)

    a, b, c, d, e = st.columns(5)
    a.metric("LTP", f"{spot:.2f}" if np.isfinite(spot) else "N/A")
    b.metric("CE OI", f"{call_oi:,.0f}")
    c.metric("PE OI", f"{put_oi:,.0f}")
    d.metric("PCR", f"{pcr:.2f}" if np.isfinite(pcr) else "N/A")
    e.metric(
        "Max Pain",
        f"{max_pain:.2f}" if np.isfinite(max_pain) else "N/A",
    )

    st.dataframe(
        df.sort_values(["Strike", "Type"]),
        use_container_width=True,
        height=550,
    )

    st.download_button(
        "📥 DOWNLOAD OPTION CHAIN EXCEL",
        _excel(df, "OPTION_CHAIN"),
        f"OPTION_CHAIN_{_ist_now().strftime('%Y%m%d_%H%M')}.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key="nse_option_excel",
    )


# ============================================================
# MAIN ENTRY POINT
# ============================================================

def show_option_chain(fyers):
    st.title("📊 Options Chain + Price Action + Buy/Sell Pressure")
    st.caption(
        "NSE / F&O + 🪙 MCX Commodity Option Chain — "
        "exact current MCX futures symbols are resolved automatically."
    )

    market = st.radio(
        "Market",
        ["🇮🇳 NSE / F&O", "🪙 MCX Commodities"],
        horizontal=True,
        key="option_chain_market",
    )

    if market == "🪙 MCX Commodities":
        show_mcx(fyers)
    else:
        show_nse_fno(fyers)
