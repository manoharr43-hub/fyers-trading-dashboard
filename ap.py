import streamlit as st
from fyers_apiv3 import fyersModel

st.set_page_config(page_title="FYERS Dashboard", layout="wide")

CLIENT_ID = st.secrets["FYERS_CLIENT_ID"]
SECRET_KEY = st.secrets["FYERS_SECRET_KEY"]
REDIRECT_URI = st.secrets["FYERS_REDIRECT_URI"]

st.title("📈 FYERS Trading Dashboard")

# Generate Login URL
session = fyersModel.SessionModel(
    client_id=CLIENT_ID,
    secret_key=SECRET_KEY,
    redirect_uri=REDIRECT_URI,
    response_type="code",
    grant_type="authorization_code"
)

params = st.query_params

# If auth code returned from FYERS
if "auth_code" in params:
    try:
        auth_code = params["auth_code"]

        session.set_token(auth_code)

        token_response = session.generate_token()

        access_token = token_response["access_token"]

        st.session_state["access_token"] = access_token

        st.success("✅ Login Successful")

        fyers = fyersModel.FyersModel(
            client_id=CLIENT_ID,
            token=access_token,
            is_async=False,
            log_path=""
        )

        profile = fyers.get_profile()

        st.subheader("👤 Profile")
        st.json(profile)

    except Exception as e:
        st.error(f"Token Error: {e}")

else:
    login_url = session.generate_authcode()

    st.markdown(
        f"""
        <a href="{login_url}">
            <button style="
                background-color:#0066ff;
                color:white;
                padding:12px 20px;
                border:none;
                border-radius:8px;">
                🔐 Login With FYERS
            </button>
        </a>
        """,
        unsafe_allow_html=True
    )
