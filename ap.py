import streamlit as st
from fyers_apiv3 import fyersModel
import pandas as pd
import numpy as np
import yfinance as yf
import os

# Page Config
st.set_page_config(page_title="NSE AI PRO V11.11", layout="wide")

# Fyers Session Setup (Streamlit Secrets లో కీస్ ఉండాలి)
client_id = st.secrets.get("FYERS_CLIENT_ID")
secret_key = st.secrets.get("FYERS_SECRET_KEY")
redirect_uri = "https://manoharr43-hub-norenrestapipy-ap-hk1emv.streamlit.app/"

if "access_token" not in st.session_state:
    st.session_state["access_token"] = None

# Sidebar Navigation
menu = st.sidebar.radio("Navigation", ["🚀 AI Scanner", "🛒 Trade Entry", "💰 Portfolio/Funds"])

# Login logic
if st.session_state["access_token"] is None:
    session = fyersModel.SessionModel(client_id=client_id, secret_key=secret_key, redirect_uri=redirect_uri, response_type="code", grant_type="authorization_code")
    query_params = st.query_params
    if "auth_code" in query_params:
        session.set_token(query_params["auth_code"])
        response = session.generate_token()
        if "access_token" in response:
            st.session_state["access_token"] = response["access_token"]
            st.rerun()
    else:
        st.markdown(f"[🔐 Fyers కి లాగిన్ అవ్వండి]({session.generate_authcode()})")
        st.stop()

# Fyers Model
fyers = fyersModel.FyersModel(client_id=client_id, token=st.session_state["access_token"], is_async=False, log_path="")

# --- NAVIGATION CONTENT ---

if menu == "🚀 AI Scanner":
    st.title("🚀 NSE AI PRO V11.11")
    # ఇక్కడ మీ పాత AI Scanner కోడ్ (Data processing, XGBoost etc.) పేస్ట్ చేయండి
    st.info("AI Scanner స్క్రీన్ సిద్ధంగా ఉంది...")

elif menu == "🛒 Trade Entry":
    st.title("🛒 Fyers Trading Console")
    symbol = st.text_input("Enter Symbol (e.g., NSE:SBIN-EQ)", "NSE:SBIN-EQ")
    qty = st.number_input("Quantity", value=1)
    
    col1, col2 = st.columns(2)
    if col1.button("Buy"):
        order = fyers.place_order(data={"symbol": symbol, "qty": qty, "type": 1, "side": 1, "productType": "INTRADAY", "validity": "DAY"})
        st.write(order)
    if col2.button("Sell"):
        order = fyers.place_order(data={"symbol": symbol, "qty": qty, "type": 1, "side": -1, "productType": "INTRADAY", "validity": "DAY"})
        st.write(order)

elif menu == "💰 Portfolio/Funds":
    st.title("💰 Portfolio & Funds")
    if st.button("Refresh Data"):
        funds = fyers.funds()
        pos = fyers.positions()
        st.subheader("Funds")
        st.write(funds)
        st.subheader("Positions")
        st.write(pos)
