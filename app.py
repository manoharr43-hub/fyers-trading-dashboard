import streamlit as st
import os
from fyers_apiv3 import fyersModel

st.set_page_config(
    page_title="FYERS Dashboard",
    layout="wide"
)

CLIENT_ID = os.getenv("FYERS_CLIENT_ID", st.secrets.get("FYERS_CLIENT_ID", ""))
SECRET_KEY = os.getenv("FYERS_SECRET_KEY", st.secrets.get("FYERS_SECRET_KEY", ""))
REDIRECT_URI = os.getenv("FYERS_REDIRECT_URI", st.secrets.get("FYERS_REDIRECT_URI", ""))

st.title("📈 FYERS Trading Dashboard")

if not CLIENT_ID or not SECRET_KEY or not REDIRECT_URI:
    st.error("FYERS credentials are missing.")
    st.stop()

session = fyersModel.SessionModel(
    client_id=CLIENT_ID,
    secret_key=SECRET_KEY,
    redirect_uri=REDIRECT_URI,
    response_type="code",
    grant_type="authorization_code"
)

login_url = session.generate_authcode()

st.success("Click below to login with FYERS")
st.link_button("Login to FYERS", login_url)
