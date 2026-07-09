import streamlit as st
from fyers_apiv3 import fyersModel

# ==========================================
# PAGE CONFIG
# ==========================================
st.set_page_config(
    page_title="NSE AI PRO V12",
    page_icon="📈",
    layout="wide"
)

# ==========================================
# IMPORT MODULES
# ==========================================
try:
    from dashboard import show_dashboard
    from market import show_market
    from portfolio import show_portfolio
    from orders import show_orders
    from option_chain import show_option_chain
    from scanner import show_scanner
    from charts import show_charts
    from trading import show_trading
    from profile import show_profile
    from settings import show_settings

    # NEW MODULE
    from ai_market_intelligence import show_ai_market_intelligence

except ImportError as e:
    st.error(f"Module Import Error : {e}")
    st.info("All .py files must be inside the same folder.")
    st.stop()

# ==========================================
# FYERS CONFIG
# ==========================================
CLIENT_ID = st.secrets["FYERS_CLIENT_ID"]
SECRET_KEY = st.secrets["FYERS_SECRET_KEY"]
REDIRECT_URI = st.secrets["FYERS_REDIRECT_URI"]

# ==========================================
# SESSION
# ==========================================
if "access_token" not in st.session_state:
    st.session_state.access_token = None

if "logged_in" not in st.session_state:
    st.session_state.logged_in = False

# ==========================================
# LOGIN
# ==========================================
session = fyersModel.SessionModel(
    client_id=CLIENT_ID,
    secret_key=SECRET_KEY,
    redirect_uri=REDIRECT_URI,
    response_type="code",
    grant_type="authorization_code"
)

if not st.session_state.logged_in:

    st.title("📈 NSE AI PRO V12")

    login_url = session.generate_authcode()

    st.link_button(
        "🔑 Login With FYERS",
        login_url,
        use_container_width=True
    )

    params = st.query_params

    if "auth_code" in params:

        try:

            session.set_token(params["auth_code"])

            response = session.generate_token()

            if response["s"] == "ok":

                st.session_state.access_token = response["access_token"]
                st.session_state.logged_in = True

                st.rerun()

            else:
                st.error(response)

        except Exception as e:
            st.error(e)

    st.stop()

# ==========================================
# CONNECT FYERS
# ==========================================
fyers = fyersModel.FyersModel(
    client_id=CLIENT_ID,
    token=st.session_state.access_token,
    is_async=False
)

# ==========================================
# SIDEBAR
# ==========================================
menu = st.sidebar.radio(

    "Navigation",

    [

        "🏠 Dashboard",

        "📈 Market",

        "🧠 AI Market Intelligence",

        "💼 Portfolio",

        "📋 Orders",

        "⚙️ Option Chain",

        "🤖 Scanner",

        "📊 Charts",

        "💹 Trading",

        "👤 Profile",

        "⚙️ Settings"

    ]

)

# ==========================================
# PAGE ROUTING
# ==========================================
pages = {

    "🏠 Dashboard": show_dashboard,

    "📈 Market": show_market,

    "🧠 AI Market Intelligence": show_ai_market_intelligence,

    "💼 Portfolio": show_portfolio,

    "📋 Orders": show_orders,

    "⚙️ Option Chain": show_option_chain,

    "🤖 Scanner": show_scanner,

    "📊 Charts": show_charts,

    "💹 Trading": show_trading,

    "👤 Profile": show_profile,

    "⚙️ Settings": show_settings,

}

# ==========================================
# LOAD PAGE
# ==========================================
try:

    pages[menu](fyers)

except Exception as e:

    st.error(f"Error Loading {menu}")

    st.exception(e)

# ==========================================
# LOGOUT
# ==========================================
st.sidebar.divider()

if st.sidebar.button("🚪 Logout"):

    st.session_state.clear()

    st.rerun()
"""
app.py — Total Project Entry Point
====================================
Ties the two existing modules together into one runnable Streamlit app:

  • option_chain.py    -> show_option_chain(fyers)   (Options Chain Dashboard,
                           FYERS primary / NSE automatic fallback, AI Engine,
                           Gamma Build-up, AI Scalping Engine, Excel export)
  • market_analysis.py -> show_market(fyers)          (Quote / Market Depth /
                           Historical Data panel)

This file adds ONLY the missing piece both modules already assumed existed:
an authenticated `fyers` client instance and simple page navigation. Neither
option_chain.py nor market_analysis.py is modified — they're imported as-is.

FYERS LOGIN
-----------
You need a FYERS API v3 app (Client ID) and a valid Access Token (generated
via FYERS' OAuth login flow — this file does NOT implement the OAuth
redirect/auth-code exchange itself, since that requires a registered
redirect URI outside this app's control). Paste your Client ID + Access
Token in the sidebar once you have them, or set them as environment
variables / Streamlit secrets so you don't have to paste them every run:

    # .streamlit/secrets.toml
    FYERS_CLIENT_ID = "XXXXX-100"
    FYERS_ACCESS_TOKEN = "eyJhbGciOi..."

Run with:  streamlit run app.py
"""

import logging
import os

import streamlit as st

from option_chain import show_option_chain
from market_analysis import show_market

logger = logging.getLogger("options_chain_dashboard")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)


def _get_default(key: str) -> str:
    """Checks Streamlit secrets first, then environment variables, so
    credentials don't have to be re-typed every run if either is set."""
    try:
        if key in st.secrets:
            return str(st.secrets[key])
    except Exception:  # noqa: BLE001 - no secrets.toml present is normal, not an error
        pass
    return os.environ.get(key, "")


def _init_fyers(client_id: str, access_token: str):
    """Builds an authenticated FyersModel instance. Returns (fyers, error)
    — never raises, so a bad/expired token surfaces as a clear sidebar
    message instead of crashing the whole app on import/init."""
    try:
        from fyers_apiv3 import fyersModel
    except ImportError as e:
        return None, (
            "The `fyers-apiv3` package isn't installed. Run `pip install fyers-apiv3` "
            f"and reload. ({e})"
        )
    try:
        fyers = fyersModel.FyersModel(
            client_id=client_id,
            token=access_token,
            is_async=False,
            log_path="",
        )
        return fyers, None
    except Exception as e:  # noqa: BLE001 - external SDK, keep resilient
        logger.error("FYERS client initialization failed: %s", e)
        return None, f"Could not initialize the FYERS client: {e}"


def main():
    st.set_page_config(page_title="FYERS Trading Suite", page_icon="📊", layout="wide")

    with st.sidebar:
        st.markdown("## 🔑 FYERS Login")
        client_id = st.text_input(
            "Client ID", value=_get_default("FYERS_CLIENT_ID"),
            help="e.g. ABCDE12345-100",
        )
        access_token = st.text_input(
            "Access Token", value=_get_default("FYERS_ACCESS_TOKEN"), type="password",
            help="Generated via FYERS' OAuth login flow.",
        )
        st.caption(
            "Don't have a token yet? Generate one through FYERS' standard OAuth "
            "login flow (authorize app → exchange auth code for an access token) "
            "using your FYERS API app's Client ID and Secret Key."
        )
        st.divider()
        page = st.radio(
            "📂 Navigate",
            ["📊 Options Chain Dashboard", "📈 Live Market Analysis"],
            key="oc_app_page",
        )

    if not client_id or not access_token:
        st.info(
            "👈 Enter your FYERS **Client ID** and **Access Token** in the sidebar to get started. "
            "The Options Chain Dashboard will still work in NSE-fallback mode for supported indices "
            "(NIFTY / BANKNIFTY / FINNIFTY / MIDCPNIFTY) even without a FYERS session, since NSE is "
            "used automatically whenever FYERS is unavailable — but Live Market Analysis (quotes, "
            "depth, history) requires a valid FYERS session."
        )
        fyers = None
    else:
        fyers, err = _init_fyers(client_id, access_token)
        if err:
            st.error(err)

    if page == "📊 Options Chain Dashboard":
        # show_option_chain() already handles fyers=None-like failures
        # internally via its FYERS -> NSE automatic fallback (see
        # get_option_chain_data() in option_chain.py) — it never crashes
        # even without a valid FYERS session, for supported instruments.
        show_option_chain(fyers)
    else:
        if fyers is None:
            st.warning("Live Market Analysis needs a valid FYERS session — enter your credentials in the sidebar.")
        else:
            show_market(fyers)


if __name__ == "__main__":
    main()
