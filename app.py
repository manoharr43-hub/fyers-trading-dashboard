import streamlit as st
from fyers_apiv3 import fyersModel

# ====================================
# PAGE CONFIG
# ====================================

st.set_page_config(
    page_title="FYERS Trading Dashboard",
    page_icon="📈",
    layout="wide"
)

# ====================================
# SECRETS
# ====================================

CLIENT_ID = st.secrets["FYERS_CLIENT_ID"]
SECRET_KEY = st.secrets["FYERS_SECRET_KEY"]
REDIRECT_URI = st.secrets["FYERS_REDIRECT_URI"]

# ====================================
# TITLE
# ====================================

st.title("📈 FYERS Trading Dashboard")

# ====================================
# SESSION MODEL
# ====================================

session = fyersModel.SessionModel(
    client_id=CLIENT_ID,
    secret_key=SECRET_KEY,
    redirect_uri=REDIRECT_URI,
    response_type="code",
    grant_type="authorization_code"
)

# ====================================
# GET URL PARAMS
# ====================================

params = st.query_params

auth_code = None

if "auth_code" in params:
    auth_code = params["auth_code"]

elif "code" in params:
    auth_code = params["code"]

# ====================================
# LOGIN SUCCESS FLOW
# ====================================

if auth_code:

    try:

        session.set_token(auth_code)

        token_response = session.generate_token()

        access_token = token_response["access_token"]

        st.session_state["access_token"] = access_token

        st.success("✅ FYERS Login Successful")

        fyers = fyersModel.FyersModel(
            client_id=CLIENT_ID,
            token=access_token,
            is_async=False,
            log_path=""
        )

        # =============================
        # PROFILE
        # =============================

        profile = fyers.get_profile()

        st.subheader("👤 Profile")

        st.json(profile)

        # =============================
        # FUNDS
        # =============================

        try:

            funds = fyers.funds()

            st.subheader("💰 Funds")

            st.json(funds)

        except:

            st.warning("Funds Not Available")

        # =============================
        # HOLDINGS
        # =============================

        try:

            holdings = fyers.holdings()

            st.subheader("📦 Holdings")

            st.json(holdings)

        except:

            st.warning("Holdings Not Available")

        # =============================
        # POSITIONS
        # =============================

        try:

            positions = fyers.positions()

            st.subheader("📊 Positions")

            st.json(positions)

        except:

            st.warning("Positions Not Available")

        # =============================
        # ORDERS
        # =============================

        try:

            orders = fyers.orderbook()

            st.subheader("📝 Orders")

            st.json(orders)

        except:

            st.warning("Orders Not Available")

    except Exception as e:

        st.error(f"Login Error : {e}")

# ====================================
# LOGIN BUTTON
# ====================================

else:

    login_url = session.generate_authcode()

    st.markdown(
        f"""
        <a href="{login_url}">
            <button style="
                background:#0066ff;
                color:white;
                border:none;
                padding:15px 30px;
                border-radius:10px;
                font-size:18px;
                cursor:pointer;">
                🔐 Login With FYERS
            </button>
        </a>
        """,
        unsafe_allow_html=True
    )

    st.info("Click Login Button To Connect FYERS Account")
