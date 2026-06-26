import streamlit as st
import sys
import os

# సిస్టమ్ పాత్ సెట్టింగ్స్ (ఎర్రర్స్ రాకుండా)
sys.path.append(os.path.abspath(os.path.dirname(__file__)))

from utils.fyers_auth import get_fyers_session, generate_token

# పేజీ కాన్ఫిగరేషన్
st.set_page_config(page_title="NSE AI PRO V13", layout="wide")

# సెషన్ మేనేజ్మెంట్
if "access_token" not in st.session_state:
    st.session_state["access_token"] = None

# లాగిన్ లాజిక్
params = st.query_params
auth_code = params.get("auth_code") or params.get("code")

# టోకెన్ జనరేషన్
if auth_code and st.session_state["access_token"] is None:
    try:
        st.session_state["access_token"] = generate_token(auth_code)
        st.rerun()
    except Exception as e:
        st.error(f"Login Failed: {e}")

# లాగిన్ బటన్
if st.session_state["access_token"] is None:
    st.title("🚀 NSE AI PRO V13")
    session = get_fyers_session()
    auth_url = session.generate_authcode()
    st.markdown(f"### [🔐 CLICK HERE TO LOGIN WITH FYERS]({auth_url})", unsafe_allow_html=True)
    st.stop()

# లాగిన్ సక్సెస్ అయ్యాక
st.sidebar.success("✅ Logged in successfully!")
st.title("📊 NSE AI PRO V13 - Dashboard")
st.write("Welcome Manohar! Your terminal is ready.")
