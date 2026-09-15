import streamlit as st
from fyers_apiv3 import fyersModel

st.set_page_config(page_title="NSE AI PRO V12", page_icon="📈", layout="wide")

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
    from ai_chart_analysis import show_ai_chart_analysis
except ImportError as e:
    st.error(f"Module Import Error: {e}")
    st.info("గమనిక: అన్ని .py files ఒకే folder లో ఉండాలి.")
    st.stop()

CLIENT_ID = st.secrets["FYERS_CLIENT_ID"]
SECRET_KEY = st.secrets["FYERS_SECRET_KEY"]
REDIRECT_URI = st.secrets["FYERS_REDIRECT_URI"]

if "access_token" not in st.session_state:
    st.session_state["access_token"] = None
if "logged_in" not in st.session_state:
    st.session_state["logged_in"] = False

session = fyersModel.SessionModel(
    client_id=CLIENT_ID,
    secret_key=SECRET_KEY,
    redirect_uri=REDIRECT_URI,
    response_type="code",
    grant_type="authorization_code",
)

if not st.session_state["logged_in"]:
    st.title("📈 NSE AI PRO V12")
    login_url = session.generate_authcode()
    st.link_button("🔑 Login with FYERS", login_url, use_container_width=True)
    params = st.query_params
    if "auth_code" in params:
        try:
            session.set_token(params["auth_code"])
            response = session.generate_token()
            if response.get("s") == "ok":
                st.session_state["access_token"] = response["access_token"]
                st.session_state["logged_in"] = True
                st.rerun()
        except Exception as e:
            st.error(f"Login Error: {e}")
    st.stop()

fyers = fyersModel.FyersModel(
    client_id=CLIENT_ID,
    token=st.session_state["access_token"],
    is_async=False,
)

menu = st.sidebar.radio("Navigation", [
    "🏠 Dashboard", "📈 Market", "💼 Portfolio", "📋 Orders",
    "⚙️ Option Chain", "🤖 Scanner", "📊 Charts", "💹 Trading",
    "👤 Profile", "⚙️ Settings", "🤖 AI Chart Analysis"
])

pages = {
    "🏠 Dashboard": show_dashboard,
    "📈 Market": show_market,
    "💼 Portfolio": show_portfolio,
    "📋 Orders": show_orders,
    "⚙️ Option Chain": show_option_chain,
    "🤖 Scanner": show_scanner,
    "📊 Charts": show_charts,
    "💹 Trading": show_trading,
    "👤 Profile": show_profile,
    "⚙️ Settings": show_settings,
    "🤖 AI Chart Analysis": show_ai_chart_analysis,
}

try:
    pages[menu](fyers)
except Exception as e:
    st.error(f"Error loading {menu}: {e}")

st.sidebar.divider()
if st.sidebar.button("🚪 Logout"):
    st.session_state.clear()
    st.rerun()
