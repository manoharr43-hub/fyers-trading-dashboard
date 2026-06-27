import streamlit as st
import os
from fyers_apiv3 import fyersModel

st.set_page_config(page_title="FYERS Dashboard", layout="wide")

# 🔑 Credentials
CLIENT_ID = os.getenv("FYERS_CLIENT_ID", st.secrets.get("FYERS_CLIENT_ID", ""))
SECRET_KEY = os.getenv("FYERS_SECRET_KEY", st.secrets.get("FYERS_SECRET_KEY", ""))
REDIRECT_URI = os.getenv("FYERS_REDIRECT_URI", st.secrets.get("FYERS_REDIRECT_URI", ""))

st.title("📈 FYERS Trading Dashboard")

if not CLIENT_ID or not SECRET_KEY or not REDIRECT_URI:
    st.error("FYERS credentials are missing.")
    st.stop()

# 🔄 Session create function
def get_session():
    return fyersModel.SessionModel(
        client_id=CLIENT_ID,
        secret_key=SECRET_KEY,
        redirect_uri=REDIRECT_URI,
        response_type="code",
        grant_type="authorization_code"
    )

# 🟢 Login Flow
if "access_token" not in st.session_state:
    params = st.query_params
    code = params.get("code")

    if code:
        if isinstance(code, list):
            code = code[0]
        session = get_session()
        session.set_token(code)
        response = session.generate_token()

        if isinstance(response, dict) and "access_token" in response:
            st.session_state["access_token"] = response["access_token"]
            st.success("✅ Login Successful")
            st.rerun()
        else:
            st.error(response)
            st.stop()
    else:
        session = get_session()
        login_url = session.generate_authcode()
        st.info("Click below to login with FYERS")
        st.link_button("Login to FYERS", login_url)
        st.stop()

# 🔗 Fyers API object
fyers = fyersModel.FyersModel(
    client_id=CLIENT_ID,
    token=st.session_state["access_token"],
    is_async=False,
    log_path=""
)

# 📊 Dashboard Menu
menu = st.sidebar.radio("📌 Select Section", ["Profile", "Funds", "Holdings", "Positions"])

if menu == "Profile":
    data = fyers.get_profile()
    st.json(data)

elif menu == "Funds":
    data = fyers.funds()
    st.json(data)

elif menu == "Holdings":
    data = fyers.holdings()
    st.json(data)

elif menu == "Positions":
    data = fyers.positions()
    st.json(data)
