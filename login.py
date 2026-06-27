import streamlit as st
from fyers_apiv3 import fyersModel


def fyers_login():
    """
    FYERS OAuth Login
    Returns:
        fyers (FyersModel object) if login successful
        None otherwise
    """

    CLIENT_ID = st.secrets["FYERS_CLIENT_ID"]
    SECRET_KEY = st.secrets["FYERS_SECRET_KEY"]
    REDIRECT_URI = st.secrets["FYERS_REDIRECT_URI"]

    session = fyersModel.SessionModel(
        client_id=CLIENT_ID,
        secret_key=SECRET_KEY,
        redirect_uri=REDIRECT_URI,
        response_type="code",
        grant_type="authorization_code",
    )

    # Already Logged In
    if "access_token" in st.session_state:

        fyers = fyersModel.FyersModel(
            client_id=CLIENT_ID,
            token=st.session_state["access_token"],
            is_async=False,
            log_path=""
        )

        return fyers

    # Read Query Params
    params = st.query_params

    if "auth_code" in params:

        auth_code = params["auth_code"]

        try:

            session.set_token(auth_code)

            response = session.generate_token()

            if response["s"] == "ok":

                st.session_state["access_token"] = response["access_token"]

                st.query_params.clear()

                st.rerun()

            else:
                st.error(response)

        except Exception as e:
            st.error(e)

    else:

        login_url = session.generate_authcode()

        st.info("Login with your FYERS Account")

        st.link_button(
            "🔑 Login with FYERS",
            login_url,
            use_container_width=True
        )

    return None


def logout():

    if st.button("🚪 Logout"):

        if "access_token" in st.session_state:
            del st.session_state["access_token"]

        st.query_params.clear()

        st.rerun()
