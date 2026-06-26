import streamlit as st
from utils.fyers_auth import get_fyers_session, generate_token

st.set_page_config(page_title="NSE AI PRO V13", layout="wide")

# లాగిన్ లాజిక్
params = st.query_params
auth_code = params.get("auth_code") or params.get("code")

if "access_token" not in st.session_state:
    st.session_state["access_token"] = None

if auth_code and st.session_state["access_token"] is None:
    st.session_state["access_token"] = generate_token(auth_code)
    st.rerun()

if st.session_state["access_token"] is None:
    session = get_fyers_session()
    st.markdown(f'[🔐 LOGIN WITH FYERS]({session.generate_authcode()})', unsafe_allow_html=True)
    st.stop()

st.success("Logged in successfully! Welcome to NSE AI PRO V13.")
