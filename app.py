import streamlit as st
from fyers_apiv3 import fyersModel

# ==========================
# Import Pages
# ==========================
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

# ==========================
# PAGE CONFIG
# ==========================
st.set_page_config(
    page_title="FYERS Trading Dashboard",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ==========================
# FYERS CONFIG
# ==========================
CLIENT_ID = st.secrets["FYERS_CLIENT_ID"]
SECRET_KEY = st.secrets["FYERS_SECRET_KEY"]
REDIRECT_URI = st.secrets["FYERS_REDIRECT_URI"]

# ==========================
# SESSION DEFAULTS
# ==========================
if "access_token" not in st.session_state:
    st.session_state["access_token"] = None

if "logged_in" not in st.session_state:
    st.session_state["logged_in"] = False

# ==========================
# APP TITLE
# ==========================
st.title("📈 FYERS Trading Dashboard")
st.caption("Powered by FYERS API V3")

# ==========================
# CREATE FYERS SESSION
# ==========================
session = fyersModel.SessionModel(
    client_id=CLIENT_ID,
    secret_key=SECRET_KEY,
    redirect_uri=REDIRECT_URI,
    response_type="code",
    grant_type="authorization_code"
)

# ==========================
# LOGIN PAGE
# ==========================
if not st.session_state["logged_in"]:

    st.subheader("🔐 Login")

    login_url = session.generate_authcode()

    st.link_button(
        "🔑 Login with FYERS",
        login_url,
        use_container_width=True
    )

    params = st.query_params

    if "auth_code" in params:

        try:

            auth_code = params["auth_code"]

            session.set_token(auth_code)

            response = session.generate_token()

            if response.get("s") == "ok":

                st.session_state["access_token"] = response["access_token"]

                st.session_state["logged_in"] = True

                st.query_params.clear()

                st.success("Login Successful")

                st.rerun()

            else:

                st.error(response)

        except Exception as e:

            st.exception(e)

    st.stop()

# ==========================
# CREATE FYERS OBJECT
# ==========================
fyers = fyersModel.FyersModel(
    client_id=CLIENT_ID,
    token=st.session_state["access_token"],
    is_async=False,
    log_path=""
)# =====================================================
# SIDEBAR
# =====================================================

st.sidebar.image(
    "https://assets.fyers.in/images/logo.svg",
    width=180
)

st.sidebar.success("✅ Logged in")

st.sidebar.divider()

menu = st.sidebar.radio(
    "Navigation",
    [
        "🏠 Dashboard",
        "📈 Market",
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

st.sidebar.divider()

# =====================================================
# PAGE ROUTING
# =====================================================

try:

    if menu == "🏠 Dashboard":

        show_dashboard(fyers)

    elif menu == "📈 Market":

        show_market(fyers)

    elif menu == "💼 Portfolio":

        show_portfolio(fyers)

    elif menu == "📋 Orders":

        show_orders(fyers)

    elif menu == "⚙️ Option Chain":

        show_option_chain(fyers)

    elif menu == "🤖 Scanner":

        show_scanner(fyers)

    elif menu == "📊 Charts":

        show_charts(fyers)

    elif menu == "💹 Trading":

        show_trading(fyers)

    elif menu == "👤 Profile":

        show_profile(fyers)

    elif menu == "⚙️ Settings":

        show_settings()

except Exception as e:

    st.error("Error Loading Module")

    st.exception(e# =====================================================
# SIDEBAR STATUS
# =====================================================

st.sidebar.divider()

st.sidebar.subheader("📊 Account Status")

try:
    profile = fyers.get_profile()

    if profile.get("s") == "ok":

        data = profile.get("data", {})

        st.sidebar.success(
            f"👤 {data.get('display_name', 'FYERS User')}"
        )

        st.sidebar.caption(
            f"Client ID : {data.get('fy_id', '-')}"
        )

except:
    st.sidebar.warning("Profile not available")

# =====================================================
# REFRESH
# =====================================================

if st.sidebar.button("🔄 Refresh Dashboard"):
    st.rerun()

# =====================================================
# LOGOUT
# =====================================================

if st.sidebar.button("🚪 Logout"):

    st.session_state.clear()

    st.query_params.clear()

    st.success("Logged Out Successfully")

    st.rerun()

# =====================================================
# FOOTER
# =====================================================

st.divider()

col1, col2, col3 = st.columns(3)

with col1:
    st.caption("FYERS API V3")

with col2:
    st.caption("Streamlit Dashboard")

with col3:
    st.caption("Version 1.0")

st.markdown(
    """
    <center>
    <h5>
    🚀 FYERS Trading Dashboard
    </h5>

    Live Market • Portfolio • Orders • Option Chain • Scanner • Charts
    </center>
    """,
    unsafe_allow_html=True
)
