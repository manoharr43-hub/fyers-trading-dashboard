import streamlit as st
from fyers_apiv3 import fyersModel

# ==========================================
# PAGE CONFIG (call once, before any other st.* call)
# ==========================================
st.set_page_config(
    page_title="NSE AI PRO V12",
    page_icon="📈",
    layout="wide",
)

# ==========================================
# FYERS CONFIG (defensive — never crash the whole app on a missing secret)
# ==========================================
REQUIRED_SECRETS = ["FYERS_CLIENT_ID", "FYERS_SECRET_KEY", "FYERS_REDIRECT_URI"]
missing = [k for k in REQUIRED_SECRETS if k not in st.secrets]
if missing:
    st.error(
        "Missing required secrets in `.streamlit/secrets.toml`: "
        + ", ".join(missing)
        + ". Add them and rerun the app."
    )
    st.stop()

CLIENT_ID = st.secrets["FYERS_CLIENT_ID"]
SECRET_KEY = st.secrets["FYERS_SECRET_KEY"]
REDIRECT_URI = st.secrets["FYERS_REDIRECT_URI"]

# ==========================================
# SESSION & LOGIN
# ==========================================
if "access_token" not in st.session_state:
    st.session_state.access_token = None
if "logged_in" not in st.session_state:
    st.session_state.logged_in = False

session = fyersModel.SessionModel(
    client_id=CLIENT_ID,
    secret_key=SECRET_KEY,
    redirect_uri=REDIRECT_URI,
    response_type="code",
    grant_type="authorization_code",
)

if not st.session_state.logged_in:
    st.title("📈 NSE AI PRO V12")
    st.caption("Institutional-style F&O AI Decision Engine, powered by your Fyers account.")

    try:
        login_url = session.generate_authcode()
        st.link_button("🔑 Login With FYERS", login_url, use_container_width=True)
    except Exception as e:
        st.error("Could not generate the Fyers login link. Check your Fyers app credentials.")
        st.exception(e)
        st.stop()

    params = st.query_params
    if "auth_code" in params:
        try:
            session.set_token(params["auth_code"])
            response = session.generate_token()
            if response.get("s") == "ok":
                st.session_state.access_token = response["access_token"]
                st.session_state.logged_in = True
                st.query_params.clear()
                st.rerun()
            else:
                st.error(f"Fyers login failed: {response}")
        except Exception as e:
            st.error("Something went wrong while completing Fyers login.")
            st.exception(e)
    st.stop()

# ==========================================
# LAZY LOADING FUNCTION (page modules are only imported when opened, so a
# missing/broken page never breaks the rest of the app — see try/except
# in the render call below)
# ==========================================
def render_page(menu, fyers):
    if menu == "🏠 Dashboard":
        from dashboard import show_dashboard
        show_dashboard(fyers)
    elif menu == "📈 Market":
        from market import show_market
        show_market(fyers)
    elif menu == "🧠 AI Market Intelligence":
        from ai_market_intelligence import show_ai_market_intelligence
        show_ai_market_intelligence(fyers)
    elif menu == "💼 Portfolio":
        from portfolio import show_portfolio
        show_portfolio(fyers)
    elif menu == "📋 Orders":
        from orders import show_orders
        show_orders(fyers)
    elif menu == "⚙️ Option Chain":
        from option_chain import show_option_chain
        show_option_chain(fyers)
    elif menu == "🤖 Scanner":
        from scanner import show_scanner
        show_scanner(fyers)
    elif menu == "📊 Charts":
        from charts import show_charts
        show_charts(fyers)
    elif menu == "💹 Trading":
        from trading import show_trading
        show_trading(fyers)
    elif menu == "👤 Profile":
        from profile import show_profile
        show_profile(fyers)
    elif menu == "⚙️ Settings":
        from settings import show_settings
        show_settings(fyers)


# ==========================================
# MAIN APP
# ==========================================
try:
    fyers = fyersModel.FyersModel(
        client_id=CLIENT_ID,
        token=st.session_state.access_token,
        is_async=False,
    )
except Exception as e:
    st.error("Could not initialize the Fyers client with your access token. Please log in again.")
    st.exception(e)
    if st.button("🔁 Reset login"):
        st.session_state.clear()
        st.rerun()
    st.stop()

menu = st.sidebar.radio("Navigation", [
    "🏠 Dashboard", "📈 Market", "🧠 AI Market Intelligence", "💼 Portfolio",
    "📋 Orders", "⚙️ Option Chain", "🤖 Scanner", "📊 Charts", "💹 Trading",
    "👤 Profile", "⚙️ Settings",
])

try:
    render_page(menu, fyers)
except ModuleNotFoundError as e:
    st.error(f"The `{menu}` page module isn't available yet ({e}).")
except Exception as e:
    st.error(f"Error Loading {menu}")
    st.exception(e)

st.sidebar.divider()
if st.sidebar.button("🚪 Logout"):
    st.session_state.clear()
    st.rerun()
