"""
app.py - NSE AI PRO modular Streamlit launcher

Purpose:
- Keep scanner/analytics modules separate.
- Make every available module selectable from the sidebar instead of relying on
  a long horizontal st.tabs() row.
- Fail gracefully when an optional module is missing or has a different entry
  function name.

Expected project files (optional):
  login.py, dashboard.py, market.py, option_chain.py, orders.py, portfolio.py,
  charts.py, ai_analysis_engine.py, ai_market_intelligence.py, AI.py,
  Fno engine - PY.py / equivalent, scanner modules.
"""

from __future__ import annotations

import importlib
import inspect
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Any, Callable, Optional

import streamlit as st


# -----------------------------------------------------------------------------
# PAGE CONFIG
# -----------------------------------------------------------------------------
st.set_page_config(
    page_title="NSE AI PRO — Multi Scanner",
    page_icon="🚀",
    layout="wide",
    initial_sidebar_state="expanded",
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("NSE_AI_PRO_APP")


# -----------------------------------------------------------------------------
# IST
# -----------------------------------------------------------------------------
def ist_now() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Kolkata"))
    except Exception:
        return datetime.now(timezone(timedelta(hours=5, minutes=30)))


# -----------------------------------------------------------------------------
# SAFE IMPORTS
# -----------------------------------------------------------------------------
MODULES = {
    "login": "login",
    "dashboard": "dashboard",
    "market": "market",
    "option_chain": "option_chain",
    "orders": "orders",
    "portfolio": "portfolio",
    "charts": "charts",
    "ai_analysis": "ai_analysis_engine",
    "ai_market": "ai_market_intelligence",
    "ai": "AI",
}


def load_module(module_name: str):
    try:
        return importlib.import_module(module_name)
    except Exception as exc:
        logger.warning("Could not import %s: %s", module_name, exc)
        return None


LOADED = {key: load_module(name) for key, name in MODULES.items()}


# -----------------------------------------------------------------------------
# FYERS CLIENT HELPERS
# -----------------------------------------------------------------------------
def secret(name: str, default: str = "") -> str:
    value = os.getenv(name, "")
    if value:
        return str(value).strip()
    try:
        value = st.secrets.get(name, default)
        return str(value).strip() if value is not None else default
    except Exception:
        return default


def build_fyers_from_token(token: str):
    if not token:
        return None
    try:
        from fyers_apiv3 import fyersModel
        app_id = secret("FYERS_APP_ID")
        if not app_id:
            return None
        return fyersModel.FyersModel(
            client_id=app_id,
            token=token,
            is_async=False,
            log_path="",
        )
    except Exception as exc:
        logger.warning("FYERS client creation failed: %s", exc)
        return None


def get_fyers():
    """Reuse a client created by another module when possible."""
    existing = st.session_state.get("fyers_client")
    if existing is not None:
        return existing

    token = st.session_state.get("fyers_access_token") or secret("FYERS_ACCESS_TOKEN")
    if token:
        client = build_fyers_from_token(token)
        if client is not None:
            st.session_state["fyers_access_token"] = token
            st.session_state["fyers_client"] = client
            st.session_state["fyers_connected"] = True
            return client

    # Try common login entry points, but do not require any particular login API.
    login_mod = LOADED.get("login")
    if login_mod is not None:
        for name in (
            "get_fyers",
            "get_fyers_client",
            "get_fyers_connection",
            "create_fyers_client",
            "connect_fyers",
            "login_fyers",
        ):
            fn = getattr(login_mod, name, None)
            if callable(fn):
                try:
                    client = fn()
                    if client is not None:
                        st.session_state["fyers_client"] = client
                        st.session_state["fyers_connected"] = True
                        return client
                except TypeError:
                    # Function may require no arguments but have an incompatible
                    # signature; continue trying the next known name.
                    pass
                except Exception as exc:
                    logger.warning("Login helper %s failed: %s", name, exc)

    return None


# -----------------------------------------------------------------------------
# GENERIC MODULE ENTRY-POINT DISCOVERY
# -----------------------------------------------------------------------------
ENTRY_NAMES = (
    "show",
    "run",
    "main",
    "render",
    "display",
    "show_dashboard",
    "show_scanner",
    "show_market",
    "show_option_chain",
    "show_orders",
    "show_portfolio",
    "show_charts",
    "show_ai",
    "show_ai_analysis",
    "show_ai_market_intelligence",
    "run_dashboard",
)


def find_entry(module: Any, preferred: tuple[str, ...] = ()) -> Optional[Callable]:
    if module is None:
        return None

    names = preferred + ENTRY_NAMES
    seen = set()
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        fn = getattr(module, name, None)
        if callable(fn):
            return fn

    # Last resort: pick a public zero/one-argument function with a likely UI name.
    candidates = []
    for name, obj in inspect.getmembers(module, inspect.isfunction):
        if name.startswith("_"):
            continue
        low = name.lower()
        if any(word in low for word in ("show", "run", "dashboard", "scanner", "render", "main")):
            candidates.append(obj)
    return candidates[0] if candidates else None


def call_entry(fn: Callable, fyers: Any = None) -> bool:
    """Call an entry point with fyers only when its signature supports it."""
    try:
        sig = inspect.signature(fn)
        params = list(sig.parameters.values())
        positional = [
            p for p in params
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
        ]
        has_varargs = any(p.kind == p.VAR_POSITIONAL for p in params)

        if fyers is not None and (has_varargs or len(positional) >= 1):
            fn(fyers)
        else:
            fn()
        return True
    except TypeError:
        # A few older modules use a different optional signature. Try no args
        # before showing an error.
        try:
            fn()
            return True
        except Exception as exc:
            st.error(f"Module entry-point failed: {exc}")
            logger.exception("Module entry-point failed")
            return False
    except Exception as exc:
        st.error(f"Module entry-point failed: {exc}")
        logger.exception("Module entry-point failed")
        return False


# -----------------------------------------------------------------------------
# SCANNER DISCOVERY
# -----------------------------------------------------------------------------
def load_scanner_module():
    """Prefer the user's current V17 scanner implementation."""
    candidates = (
        "scanner_v17_pin_rules_button_fixed",
        "scanner_v17",
        "scanner_v17_pin_amd_all_fixed",
        "NSE_AI_PRO_V17_FIXED",
        "NSE_AI_PRO_V17_Updated",
        "NSE_AI_PRO_V17_WITH_RUN_BUTTONS",
        "NSE_AI_PRO_V17_DEVELOPING_CONFIRMED",
        "NSE_AI_PRO_Live_Movement_Scanner_FULL_FIXED",
        "Momentum_Movers_Strict_Full_Code",
        "scanner",
    )
    for name in candidates:
        try:
            mod = importlib.import_module(name)
            if find_entry(mod, ("show_scanner",)) is not None:
                return name, mod
        except Exception as exc:
            logger.debug("Scanner candidate %s unavailable: %s", name, exc)
    return None, None


SCANNER_NAME, SCANNER = load_scanner_module()


# -----------------------------------------------------------------------------
# CSS — SIDEBAR NAVIGATION FIX
# -----------------------------------------------------------------------------
st.markdown(
    """
    <style>
    [data-testid="stSidebar"] { min-width: 290px; max-width: 330px; }
    .app-title { font-size: 1.65rem; font-weight: 800; margin-bottom: .1rem; }
    .app-subtitle { opacity: .78; margin-bottom: 1rem; }
    .module-ok { padding: .35rem .55rem; border-radius: .45rem; }
    </style>
    """,
    unsafe_allow_html=True,
)


# -----------------------------------------------------------------------------
# SIDEBAR
# -----------------------------------------------------------------------------
st.sidebar.markdown("# 🚀 NSE AI PRO")
st.sidebar.caption("Multi Scanner Control Panel")
st.sidebar.caption(ist_now().strftime("%d-%b-%Y %H:%M:%S IST"))


menu = [
    "🏠 HOME",
    "🔍 NSE / F&O SCANNER",
    "⚡ MOMENTUM / LIVE",
    "📊 MARKET",
    "🎯 OPTION CHAIN",
    "🧠 AI ANALYSIS",
    "🤖 AI MARKET INTELLIGENCE",
    "📈 CHARTS",
    "📦 ORDERS",
    "💼 PORTFOLIO",
    "⚙️ SETTINGS",
]

# Use a new state key so an old Streamlit session that was sitting on
# OPTION CHAIN cannot keep reopening that page after this app.py is deployed.
selected = st.sidebar.radio(
    "SCANNER MENU",
    menu,
    index=1,
    key="main_app_menu_v2",
)


with st.sidebar.expander("🔌 MODULE STATUS", expanded=False):
    for key, module_name in MODULES.items():
        status = "✅" if LOADED.get(key) is not None else "⚠️"
        st.write(f"{status} `{module_name}.py`")
    if SCANNER is not None:
        st.write(f"✅ `{SCANNER_NAME}.py` scanner")
    else:
        st.write("⚠️ Scanner module not found")


# -----------------------------------------------------------------------------
# HEADER
# -----------------------------------------------------------------------------
st.markdown('<div class="app-title">🚀 NSE AI PRO — Multi Scanner</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="app-subtitle">NSE + F&O + Momentum + Market + Options + AI — all scanners in one app</div>',
    unsafe_allow_html=True,
)


# -----------------------------------------------------------------------------
# HOME
# -----------------------------------------------------------------------------
if selected == "🏠 HOME":
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("NSE Scanner", "READY" if SCANNER else "NOT FOUND")
    c2.metric("F&O", "READY" if SCANNER else "CHECK")
    c3.metric("Option Chain", "READY" if LOADED.get("option_chain") else "NOT FOUND")
    c4.metric("FYERS", "CONNECTED" if st.session_state.get("fyers_connected") else "NOT CONNECTED")

    st.info(
        "👈 Sidebar lo scanner select cheyyandi. Long horizontal tabs badulu "
        "sidebar navigation use chestunnam, kabatti scanner tabs hide/clipped avvakunda "
        "anni modules selectable ga untayi."
    )

    st.markdown("### 📋 Available Scanners / Modules")
    st.dataframe(
        [
            {"Module": "NSE / F&O Scanner", "Purpose": "NSE + F&O stock signals"},
            {"Module": "Momentum / Live", "Purpose": "Big Buy / Big Sell / movement"},
            {"Module": "Market", "Purpose": "Market overview"},
            {"Module": "Option Chain", "Purpose": "PCR / OI / strike analysis"},
            {"Module": "AI Analysis", "Purpose": "AI chart / signal analysis"},
            {"Module": "AI Market Intelligence", "Purpose": "Market intelligence"},
            {"Module": "Charts", "Purpose": "Price charts"},
            {"Module": "Orders", "Purpose": "Order management"},
            {"Module": "Portfolio", "Purpose": "Portfolio"},
        ],
        use_container_width=True,
        hide_index=True,
    )


# -----------------------------------------------------------------------------
# GENERIC MODULE PAGE RENDERER
# -----------------------------------------------------------------------------
def render_module_page(label: str, key: str, preferred: tuple[str, ...], needs_fyers: bool = True):
    module = LOADED.get(key)
    if module is None:
        st.error(f"❌ `{MODULES[key]}.py` module not available.")
        return
    fn = find_entry(module, preferred)
    if fn is None:
        st.error(f"❌ Entry function not found in `{MODULES[key]}.py`.")
        st.caption("Expected a public show/run/main/render function.")
        return
    fyers = get_fyers() if needs_fyers else None
    call_entry(fn, fyers)


# -----------------------------------------------------------------------------
# PAGE DISPATCH
# -----------------------------------------------------------------------------
if selected == "🏠 HOME":
    # HOME is intentionally light; it does not start any scanner.
    pass

elif selected == "🔍 NSE / F&O SCANNER":
    if SCANNER is None:
        st.error("❌ Scanner module dorakaledu.")
        st.info("Keep your scanner .py file in the same folder as app.py.")
    else:
        fyers = get_fyers()
        if fyers is None:
            st.warning("⚠️ FYERS client not connected. Scanner may require login/token.")
        fn = find_entry(SCANNER, ("show_scanner",))
        if fn is not None:
            call_entry(fn, fyers)
        else:
            st.error("❌ show_scanner() entry point not found in scanner module.")

elif selected == "⚡ MOMENTUM / LIVE":
    if SCANNER is None:
        st.error("❌ Scanner module not found.")
    else:
        fyers = get_fyers()
        fn = find_entry(SCANNER, ("show_momentum", "show_momentum_scanner", "run_momentum_dashboard"))
        if fn:
            call_entry(fn, fyers)
        else:
            st.info("Dedicated Momentum UI function not found; opening the main scanner.")
            main_fn = find_entry(SCANNER, ("show_scanner",))
            if main_fn:
                call_entry(main_fn, fyers)

elif selected == "📊 MARKET":
    render_module_page(selected, "market", ("show_market", "show_dashboard", "run_market"))

elif selected == "🎯 OPTION CHAIN":
    render_module_page(selected, "option_chain", ("show_option_chain", "run_dashboard", "run_option_chain"))

elif selected == "🧠 AI ANALYSIS":
    render_module_page(selected, "ai_analysis", ("show_ai_analysis", "run_ai_analysis", "main"), needs_fyers=False)

elif selected == "🤖 AI MARKET INTELLIGENCE":
    render_module_page(selected, "ai_market", ("show_ai_market_intelligence", "run_ai_market_intelligence", "main"), needs_fyers=False)

elif selected == "📈 CHARTS":
    render_module_page(selected, "charts", ("show_charts", "show_chart", "run_charts"))

elif selected == "📦 ORDERS":
    render_module_page(selected, "orders", ("show_orders", "run_orders", "main"))

elif selected == "💼 PORTFOLIO":
    render_module_page(selected, "portfolio", ("show_portfolio", "run_portfolio", "main"))

elif selected == "⚙️ SETTINGS":
    st.subheader("⚙️ Application Settings")

    st.markdown("### FYERS")
    app_id = secret("FYERS_APP_ID")
    redirect_uri = secret("FYERS_REDIRECT_URI")
    token_present = bool(st.session_state.get("fyers_access_token") or secret("FYERS_ACCESS_TOKEN"))

    st.write(f"FYERS_APP_ID: {'✅ configured' if app_id else '❌ missing'}")
    st.write(f"FYERS_REDIRECT_URI: {'✅ configured' if redirect_uri else '❌ missing'}")
    st.write(f"FYERS_ACCESS_TOKEN: {'✅ present' if token_present else '❌ missing'}")

    if st.button("🔄 CLEAR APP CLIENT SESSION", use_container_width=True):
        for key in ("fyers_client", "fyers_connected"):
            st.session_state.pop(key, None)
        st.success("Client session cleared. Reload/login again if required.")

    st.markdown("### 📁 Project Modules")
    for key, module_name in MODULES.items():
        if LOADED.get(key) is not None:
            st.success(f"{module_name}.py — loaded")
        else:
            st.warning(f"{module_name}.py — not loaded")

    if SCANNER is not None:
        st.success(f"Scanner — `{SCANNER_NAME}.py` loaded")
    else:
        st.warning("Scanner module — not loaded")


# -----------------------------------------------------------------------------
# FOOTER
# -----------------------------------------------------------------------------
st.divider()
st.caption(
    "NSE AI PRO • Modular launcher • Existing scanner modules are called without "
    "rewriting their internal signal logic."
)
