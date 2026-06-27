import streamlit as st
import pandas as pd
import yfinance as yf
import os
from fyers_apiv3 import fyersModel

# -------------------------------------------------
# CONFIGURATION
# -------------------------------------------------
st.set_page_config(page_title="📊 Fyers Algo Dashboard", layout="wide")

# ✅ Use your actual App ID and Secret from Fyers dashboard
CLIENT_ID = "GA68CBAJIX-100"      # Full App ID (with version)
SECRET_KEY = "M2VWI44YFG"         # Secret ID
REDIRECT_URI = "https://manoharr43-hub-norenrestapipy-ap-hk1emv.streamlit.app"  # Streamlit URL

# -------------------------------------------------
# SESSION CREATION
# -------------------------------------------------
def create_session():
    return fyersModel.SessionModel(
        client_id=CLIENT_ID,
        secret_key=SECRET_KEY,
        redirect_uri=REDIRECT_URI,
        response_type="code",
        grant_type="authorization_code"
    )

session = create_session()
params = st.query_params
st.title("📈 Fyers Algo Dashboard")

# -------------------------------------------------
# LOGIN FLOW
# -------------------------------------------------
if "access_token" not in st.session_state:
    if "code" in params:
        try:
            session.set_token(params["code"])
            token_response = session.generate_token()
            st.session_state["access_token"] = token_response["access_token"]
            st.success("✅ Login Successful — ట్రేడింగ్ సిద్ధంగా ఉంది!")
            st.rerun()
        except Exception as e:
            st.error(f"Login Error: {e}")
    else:
        auth_url = session.generate_authcode()
        st.markdown(f"[🔐 Login With Fyers]({auth_url})")
        st.stop()

# -------------------------------------------------
# FYERS OBJECT
# -------------------------------------------------
fyers = fyersModel.FyersModel(
    client_id=CLIENT_ID,
    token=st.session_state["access_token"],
    is_async=False,
    log_path=""
)

# -------------------------------------------------
# SIDEBAR MENU
# -------------------------------------------------
menu = st.sidebar.radio(
    "📋 Menu ఎంపిక చేయండి",
    ["Profile", "Funds", "Holdings", "Positions", "Place Order", "NSE Scanner"]
)

# -------------------------------------------------
# PROFILE
# -------------------------------------------------
if menu == "Profile":
    profile = fyers.get_profile()
    st.subheader("👤 Profile వివరాలు")
    st.json(profile)

# -------------------------------------------------
# FUNDS
# -------------------------------------------------
elif menu == "Funds":
    funds = fyers.funds()
    st.subheader("💰 Funds వివరాలు")
    st.json(funds)

# -------------------------------------------------
# HOLDINGS
# -------------------------------------------------
elif menu == "Holdings":
    holdings = fyers.holdings()
    st.subheader("📦 Holdings వివరాలు")
    st.json(holdings)

# -------------------------------------------------
# POSITIONS
# -------------------------------------------------
elif menu == "Positions":
    positions = fyers.positions()
    st.subheader("📊 Positions వివరాలు")
    st.json(positions)

# -------------------------------------------------
# PLACE ORDER
# -------------------------------------------------
elif menu == "Place Order":
    st.subheader("🛒 Place Order — ఆర్డర్ పెట్టండి")

    symbol = st.text_input("Symbol", "NSE:RELIANCE-EQ")
    qty = st.number_input("Quantity", 1, 10000, 1)
    side = st.selectbox("Side", ["BUY", "SELL"])
    order_type = st.selectbox("Order Type", ["MARKET", "LIMIT"])
    limit_price = st.number_input("Limit Price", 0.0)

    if st.button("Place Order"):
        data = {
            "symbol": symbol,
            "qty": qty,
            "type": 2 if order_type == "MARKET" else 1,
            "side": 1 if side == "BUY" else -1,
            "productType": "INTRADAY",
            "limitPrice": limit_price,
            "stopPrice": 0,
            "validity": "DAY",
            "offlineOrder": False
        }
        result = fyers.place_order(data)
        st.json(result)

# -------------------------------------------------
# NSE SCANNER
# -------------------------------------------------
elif menu == "NSE Scanner":
    st.subheader("🔍 Simple NSE Scanner — సిగ్నల్ చెక్")
    stocks = ["RELIANCE.NS", "TCS.NS", "INFY.NS", "ICICIBANK.NS", "HDFCBANK.NS", "SBIN.NS", "LT.NS"]
    rows = []

    with st.spinner("Scanning..."):
        for stock in stocks:
            try:
                df = yf.download(stock, period="3mo", progress=False)
                close = df["Close"]
                sma20 = close.rolling(20).mean()
                current = float(close.iloc[-1])
                ma20 = float(sma20.iloc[-1])
                signal = "Bullish 📈" if current > ma20 else "Bearish 📉"
                rows.append([stock, round(current, 2), round(ma20, 2), signal])
            except Exception:
                pass

    result_df = pd.DataFrame(rows, columns=["Stock", "Price", "20 SMA", "Signal"])
    st.dataframe(result_df, use_container_width=True)
