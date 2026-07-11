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
        width="stretch"   # ✅ FIXED: replaced use_container_width
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
