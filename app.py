import streamlit as st
import pandas as pd
import numpy as np
from fyers_apiv3 import fyersModel

# 1. PAGE CONFIG
st.set_page_config(page_title="NSE AI PRO V11.11", layout="wide")

# 2. CREDENTIALS (Secrets లో ఉన్నవి వాడండి)
CLIENT_ID = st.secrets["FYERS_CLIENT_ID"]
SECRET_KEY = st.secrets["FYERS_SECRET_KEY"]
REDIRECT_URI = st.secrets["FYERS_REDIRECT_URI"]

# 3. SESSION MANAGEMENT
if "access_token" not in st.session_state: st.session_state["access_token"] = None

def get_fyers():
    return fyersModel.FyersModel(client_id=CLIENT_ID, token=st.session_state["access_token"], is_async=False, log_path="")

# 4. LOGIN LOGIC
params = st.query_params
auth_code = params.get("auth_code") or params.get("code")

if auth_code and st.session_state["access_token"] is None:
    session = fyersModel.SessionModel(client_id=CLIENT_ID, secret_key=SECRET_KEY, 
                                      redirect_uri=REDIRECT_URI, response_type="code", grant_type="authorization_code")
    session.set_token(auth_code)
    try:
        response = session.generate_token()
        st.session_state["access_token"] = response["access_token"]
        st.rerun()
    except Exception as e:
        st.error(f"Login Error: {e}")

if st.session_state["access_token"] is None:
    session = fyersModel.SessionModel(client_id=CLIENT_ID, secret_key=SECRET_KEY, 
                                      redirect_uri=REDIRECT_URI, response_type="code", grant_type="authorization_code")
    st.markdown(f'[🔐 LOGIN WITH FYERS]({session.generate_authcode()})', unsafe_allow_html=True)
    st.stop()

# 5. NAVIGATION
menu = st.sidebar.radio("Navigation", ["Dashboard", "AI Scanner", "Trade Console"])

# DASHBOARD
if menu == "Dashboard":
    st.title("📊 Advanced Trading Dashboard")
    fyers = get_fyers()
    funds = fyers.funds()
    if funds['s'] == 'ok':
        col1, col2 = st.columns(2)
        col1.metric("Available Balance", f"₹{funds['fund_limit'][0]['equityAmount']}")
    st.subheader("💼 Active Positions")
    pos = fyers.positions()
    if pos['s'] == 'ok' and len(pos['netPositions']) > 0:
        st.dataframe(pd.DataFrame(pos['netPositions']), use_container_width=True)
    else:
        st.info("No active positions.")

# AI SCANNER
elif menu == "AI Scanner":
    st.title("🚀 NSE AI PRO V11.11 - Advanced AI")
    seg = st.sidebar.selectbox("Select Segment", ["Index", "Stocks", "F&O"])
    
    if st.button("🚀 RUN ADVANCED AI SCAN"):
        fyers = get_fyers()
        watch_list = ["NSE:NIFTY50-INDEX", "NSE:NIFTYBANK-INDEX"] if seg == "Index" else ["NSE:SBIN-EQ"]
        report = []
        for sym in watch_list:
            res = fyers.history(data={"symbol": sym, "resolution": "15", "date_format": "1", "range_from": "2026-06-24", "range_to": "2026-06-25", "cont_flag": "1"})
            if res['s'] == 'ok':
                df = pd.DataFrame(res['candles'], columns=['ts', 'o', 'h', 'l', 'c', 'v'])
                # AI Logic
                rsi = 50 # Simplified for logic
                status = "🚀 STRONG BIG MOVE" if (df['h'].max() - df['l'].min()) > (df['c'].iloc[-1] * 0.008) else "➖ SIDEWAYS"
                report.append({"Symbol": sym, "LTP": df['c'].iloc[-1], "Status": status})
        
        st.session_state.v11_master_data = pd.DataFrame(report)
        st.success("✅ Analysis Complete!")

    if not st.session_state.get("v11_master_data") is None:
        st.dataframe(st.session_state.v11_master_data, use_container_width=True)

# TRADE CONSOLE
elif menu == "Trade Console":
    st.title("🛒 Trade Console")
    symbol = st.text_input("Symbol", "NSE:SBIN-EQ")
    qty = st.number_input("Quantity", value=1)
    if st.button("Buy"):
        st.write(get_fyers().place_order(data={"symbol": symbol, "qty": qty, "type": 1, "side": 1, "productType": "INTRADAY", "validity": "DAY"}))
