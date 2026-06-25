import streamlit as st
import pandas as pd
import numpy as np
import yfinance as yf
from fyers_apiv3 import fyersModel
import io

# 1. PAGE CONFIG
st.set_page_config(page_title="NSE AI PRO V11.11", layout="wide")

# 2. FYERS SESSION & AUTH
CLIENT_ID = st.secrets["FYERS_CLIENT_ID"]
SECRET_KEY = st.secrets["FYERS_SECRET_KEY"]
REDIRECT_URI = st.secrets["FYERS_REDIRECT_URI"]

if "access_token" not in st.session_state: st.session_state["access_token"] = None

def get_fyers_instance(token):
    return fyersModel.FyersModel(client_id=CLIENT_ID, token=token, is_async=False, log_path="")

# Login Flow
params = st.query_params
auth_code = params.get("auth_code") or params.get("code")

if auth_code and not st.session_state["access_token"]:
    session = fyersModel.SessionModel(client_id=CLIENT_ID, secret_key=SECRET_KEY, 
                                      redirect_uri=REDIRECT_URI, response_type="code", grant_type="authorization_code")
    session.set_token(auth_code)
    response = session.generate_token()
    st.session_state["access_token"] = response["access_token"]
    st.rerun()

# 3. SIDEBAR NAVIGATION
st.sidebar.title("Navigation")
menu = st.sidebar.radio("Go to", ["Dashboard", "AI Scanner", "Trade Console"])

if not st.session_state["access_token"]:
    session = fyersModel.SessionModel(client_id=CLIENT_ID, secret_key=SECRET_KEY, 
                                      redirect_uri=REDIRECT_URI, response_type="code", grant_type="authorization_code")
    st.markdown(f'[🔐 Login With FYERS]({session.generate_authcode()})', unsafe_allow_html=True)
    st.stop()

fyers = get_fyers_instance(st.session_state["access_token"])

# 4. TAB LOGIC
if menu == "Dashboard":
    st.title("📈 Dashboard")
    col1, col2 = st.columns(2)
    with col1:
        st.subheader("Profile")
        st.json(fyers.get_profile())
    with col2:
        st.subheader("Funds")
        st.json(fyers.funds())
    st.subheader("Positions")
    st.json(fyers.positions())

elif menu == "AI Scanner":
    st.title("🚀 NSE AI PRO V11.11")
    # ఇక్కడ మీ పాత స్కానర్ కోడ్ లాజిక్ ఉంచండి
    st.write("స్కానర్ ఇంజన్ సిద్ధంగా ఉంది...")
    if st.button("RUN SCANNER"):
        st.success("Scan Complete: Data Ready")

elif menu == "Trade Console":
    st.title("🛒 Trade Console")
    symbol = st.text_input("Symbol", "NSE:SBIN-EQ")
    qty = st.number_input("Quantity", value=1)
    c1, c2 = st.columns(2)
    if c1.button("Buy"):
        res = fyers.place_order(data={"symbol": symbol, "qty": qty, "type": 1, "side": 1, "productType": "INTRADAY", "validity": "DAY"})
        st.write(res)
    if c2.button("Sell"):
        res = fyers.place_order(data={"symbol": symbol, "qty": qty, "type": 1, "side": -1, "productType": "INTRADAY", "validity": "DAY"})
        st.write(res)
