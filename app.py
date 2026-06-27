import streamlit as st
from fyers_apiv3 import fyersModel

st.set_page_config(page_title="FYERS Trading Dashboard", layout="wide")

# ==========================
# CONFIG
# ==========================
CLIENT_ID = st.secrets["FYERS_CLIENT_ID"]
SECRET_KEY = st.secrets["FYERS_SECRET_KEY"]
REDIRECT_URI = st.secrets["FYERS_REDIRECT_URI"]

st.title("📈 FYERS Trading Dashboard")

# ==========================
# Session Model
# ==========================
session = fyersModel.SessionModel(
    client_id=CLIENT_ID,
    secret_key=SECRET_KEY,
    redirect_uri=REDIRECT_URI,
    response_type="code",
    grant_type="authorization_code",
)

# ==========================
# Already Logged In
# ==========================
if "access_token" in st.session_state:
    st.success("✅ FYERS Login Successful")
    st.write("Access Token:")
    st.code(st.session_state["access_token"])

    if st.button("Logout"):
        del st.session_state["access_token"]
        st.rerun()

    st.stop()

# ==========================
# Read Auth Code
# ==========================
params = st.query_params

if "auth_code" in params:

    auth_code = params["auth_code"]

    try:

        session.set_token(auth_code)

        response = session.generate_token()

        if response.get("s") == "ok":

            st.session_state["access_token"] = response["access_token"]

            st.query_params.clear()

            st.rerun()

        else:
            st.error(response)

    except Exception as e:
        st.error(e)

else:

    login_url = session.generate_authcode()

    st.link_button(
        "🔑 Login with FYERS",
        login_url,
        use_container_width=True
    )
