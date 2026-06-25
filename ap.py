import streamlit as st
from fyers_apiv3 import fyersModel

# Page Config
st.set_page_config(page_title="FYERS Trading Dashboard", layout="wide", page_icon="📈")

# Secrets
CLIENT_ID = st.secrets["FYERS_CLIENT_ID"]
SECRET_KEY = st.secrets["FYERS_SECRET_KEY"]
REDIRECT_URI = st.secrets["FYERS_REDIRECT_URI"]

# Session management
if "access_token" not in st.session_state:
    st.session_state["access_token"] = None

# Sidebar Menu
st.sidebar.title("Navigation")
menu = st.sidebar.radio("Go to", ["Dashboard", "AI Scanner", "Trade Console"])

# Session Model
session = fyersModel.SessionModel(client_id=CLIENT_ID, secret_key=SECRET_KEY, 
                                  redirect_uri=REDIRECT_URI, response_type="code", grant_type="authorization_code")

# Login Logic
params = st.query_params
auth_code = params.get("auth_code") or params.get("code")

if auth_code and not st.session_state["access_token"]:
    session.set_token(auth_code)
    response = session.generate_token()
    st.session_state["access_token"] = response["access_token"]

if st.session_state["access_token"]:
    fyers = fyersModel.FyersModel(client_id=CLIENT_ID, token=st.session_state["access_token"], is_async=False, log_path="")

    if menu == "Dashboard":
        st.title("📈 FYERS Trading Dashboard")
        st.success("✅ FYERS Login Successful")
        
        col1, col2 = st.columns(2)
        with col1:
            st.subheader("👤 Profile")
            st.json(fyers.get_profile())
        with col2:
            st.subheader("💰 Funds")
            st.json(fyers.funds())
            
        st.subheader("📊 Positions")
        st.json(fyers.positions())

    elif menu == "AI Scanner":
        st.title("🚀 NSE AI PRO V11.11")
        st.info("ఇక్కడ మీ AI అనాలిసిస్ కోడ్ ఉంటుంది.")

    elif menu == "Trade Console":
        st.title("🛒 Trade Console")
        symbol = st.text_input("Symbol (e.g., NSE:SBIN-EQ)", "NSE:SBIN-EQ")
        qty = st.number_input("Quantity", value=1)
        
        c1, c2 = st.columns(2)
        if c1.button("Buy"):
            response = fyers.place_order(data={"symbol": symbol, "qty": qty, "type": 1, "side": 1, "productType": "INTRADAY", "validity": "DAY"})
            st.write(response)
        if c2.button("Sell"):
            response = fyers.place_order(data={"symbol": symbol, "qty": qty, "type": 1, "side": -1, "productType": "INTRADAY", "validity": "DAY"})
            st.write(response)

else:
    login_url = session.generate_authcode()
    st.markdown(f'<a href="{login_url}" target="_self">🔐 Login With FYERS</a>', unsafe_allow_html=True)
