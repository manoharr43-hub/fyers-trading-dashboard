import streamlit as st
from fyers_apiv3 import fyersModel

# =====================================
# PAGE CONFIG
# =====================================
st.set_page_config(
    page_title="FYERS Trading Dashboard",
    page_icon="📈",
    layout="wide"
)

st.title("📈 FYERS Trading Dashboard")

# =====================================
# LOAD SECRETS
# =====================================
try:
    CLIENT_ID = st.secrets["FYERS_CLIENT_ID"]
    SECRET_KEY = st.secrets["FYERS_SECRET_KEY"]
    REDIRECT_URI = st.secrets["FYERS_REDIRECT_URI"]
except Exception as e:
    st.error(f"Secrets Error: {e}")
    st.stop()

# =====================================
# DEBUG
# =====================================
with st.expander("🔍 Debug Info"):
    st.write("CLIENT_ID:", CLIENT_ID)
    st.write("REDIRECT_URI:", REDIRECT_URI)

# =====================================
# FYERS LOGIN
# =====================================
try:

    session = fyersModel.SessionModel(
        client_id=CLIENT_ID,
        secret_key=SECRET_KEY,
        redirect_uri=REDIRECT_URI,
        response_type="code",
        grant_type="authorization_code"
    )

    login_url = session.generate_authcode()

    st.success("FYERS Login Ready")

    st.markdown(
        f"""
        <a href="{login_url}" target="_self">
            <button style="
                background:#0066ff;
                color:white;
                padding:12px 25px;
                border:none;
                border-radius:8px;
                cursor:pointer;
                font-size:16px;">
                🔐 Login With FYERS
            </button>
        </a>
        """,
        unsafe_allow_html=True
    )

except Exception as e:
    st.error(f"Login URL Error: {e}")
