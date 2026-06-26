import streamlit as st
import sys
import os

# సిస్టమ్ పాత్ సెట్టింగ్స్
sys.path.append(os.path.abspath(os.path.dirname(__file__)))

from utils.fyers_auth import get_fyers_session, generate_token

# పేజీ కాన్ఫిగరేషన్
st.set_page_config(page_title="NSE AI PRO V13", layout="wide")

# సెషన్ మేనేజ్మెంట్ - access_token ని స్టోర్ చేయడానికి
if "access_token" not in st.session_state:
    st.session_state["access_token"] = None

# లాగిన్ లాజిక్
params = st.query_params

# Fyers నుండి 'code' వచ్చినప్పుడు దాన్ని ప్రాసెస్ చేయడం
if "code" in params and st.session_state["access_token"] is None:
    auth_code = params["code"]
    try:
        # టోకెన్ జనరేట్ చేయడం
        token = generate_token(auth_code)
        st.session_state["access_token"] = token
        
        # URL నుండి కోడ్ తీసివేయడం (ఇది లూప్ అవ్వకుండా ఆపుతుంది)
        st.query_params.clear() 
        st.rerun()
    except Exception as e:
        st.error(f"Login Failed: {e}")

# లాగిన్ బటన్ - లాగిన్ అవ్వనప్పుడు మాత్రమే కనిపిస్తుంది
if st.session_state["access_token"] is None:
    st.title("🚀 NSE AI PRO V13")
    session = get_fyers_session()
    auth_url = session.generate_authcode()
    st.markdown(f"### [🔐 CLICK HERE TO LOGIN WITH FYERS]({auth_url})", unsafe_allow_html=True)
    st.stop()

# లాగిన్ సక్సెస్ అయ్యాక Dashboard
st.sidebar.success("✅ Logged in successfully!")
st.title("📊 NSE AI PRO V13 - Dashboard")
st.write("Welcome Manohar! Your terminal is ready.")
