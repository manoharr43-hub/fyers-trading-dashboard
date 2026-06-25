import streamlit as st
import pandas as pd
import yfinance as yf
import os
from fyers_apiv3 import fyersModel

# ==================================================
# CONFIG
# ==================================================
st.set_page_config(
    page_title="Fyers Trading Dashboard",
    page_icon="📈",
    layout="wide"
)

CLIENT_ID = os.getenv("FYERS_CLIENT_ID", st.secrets.get("FYERS_CLIENT_ID", ""))
SECRET_KEY = os.getenv("FYERS_SECRET_KEY", st.secrets.get("FYERS_SECRET_KEY", ""))

REDIRECT_URI = "https://manoharr43-hub-fyers-trading-dashboard.streamlit.app/"

if not CLIENT_ID:
    st.error("FYERS_CLIENT_ID Missing")
    st.stop()

if not SECRET_KEY:
    st.error("FYERS_SECRET_KEY Missing")
    st.stop()

# ==================================================
# LOGIN SESSION
# ==================================================
def get_session():
    return fyersModel.SessionModel(
        client_id=CLIENT_ID,
        secret_key=SECRET_KEY,
        redirect_uri=REDIRECT_URI,
        response_type="code",
        grant_type="authorization_code"
    )

st.title("📈 Fyers Trading Dashboard")

# ==================================================
# LOGIN
# ==================================================
if "access_token" not in st.session_state:
    params = st.query_params
    code = params.get("code")

    if code:
        try:
            session = get_session()
            if isinstance(code, list):
                code = code[0]
            session.set_token(code)
            response = session.generate_token()

            if isinstance(response, dict) and "access_token" in response:
                st.session_state["access_token"] = response["access_token"]
                st.success("Login Successful ✅")
                st.rerun()
            else:
                st.error(response)
                st.info("Please login again")
                st.stop()
        except Exception as e:
            st.error(f"Login Error: {e}")
            st.stop()
    else:
        session = get_session()
        auth_url = session.generate_authcode()
        st.markdown(f"### Login Required\n[🔐 Login With Fyers]({auth_url})")
        st.stop()

# ==================================================
# FYERS OBJECT
# ==================================================
try:
    fyers = fyersModel.FyersModel(
        client_id=CLIENT_ID,
        token=st.session_state["access_token"],
        is_async=False,
        log_path=""
    )
except Exception as e:
    st.error(e)
    st.stop()

# ==================================================
# SIDEBAR MENU
# ==================================================
menu = st.sidebar.radio(
    "Menu",
    ["Profile", "Funds", "Holdings", "Positions", "Scanner"]
)

# ==================================================
# PROFILE
# ==================================================
if menu == "Profile":
    st.subheader("👤 Profile")
    try:
        st.json(fyers.get_profile())
    except Exception as e:
        st.error(e)

# ==================================================
# FUNDS
# ==================================================
elif menu == "Funds":
    st.subheader("💰 Funds")
    try:
        st.json(fyers.funds())
    except Exception as e:
        st.error(e)

# ==================================================
# HOLDINGS
# ==================================================
elif menu == "Holdings":
    st.subheader("📦 Holdings")
    try:
        st.json(fyers.holdings())
    except Exception as e:
        st.error(e)

# ==================================================
# POSITIONS
# ==================================================
elif menu == "Positions":
    st.subheader("📊 Positions")
    try:
        st.json(fyers.positions())
    except Exception as e:
        st.error(e)

# ==================================================
# NSE SCANNER
# ==================================================
elif menu == "Scanner":
    st.subheader("📊 NSE Scanner")

    stocks = [
        "RELIANCE.NS", "TCS.NS", "INFY.NS",
        "HDFCBANK.NS", "ICICIBANK.NS", "SBIN.NS", "LT.NS"
    ]

    def calculate_rsi(series, period=14):
        delta = series.diff()
        gain = delta.where(delta > 0, 0)
        loss = -delta.where(delta < 0, 0)
        avg_gain = gain.rolling(period).mean()
        avg_loss = loss.rolling(period).mean()
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    results = []
    with st.spinner("🔍 Scanning Stocks..."):
        for stock in stocks:
            try:
                df = yf.download(
                    stock,
                    period="6mo",
                    interval="1d",
                    auto_adjust=True,
                    progress=False
                )
                if len(df) < 50:
                    continue

                close = df["Close"]
                sma20 = close.rolling(20).mean()
                sma50 = close.rolling(50).mean()
                rsi = calculate_rsi(close).iloc[-1]

                current = round(float(close.values[-1]), 2)
                ma20 = round(float(sma20.values[-1]), 2)
                ma50 = round(float(sma50.values[-1]), 2)
                rsi_val = round(float(rsi), 2)

                if current > ma20 and ma20 > ma50 and rsi_val > 60:
                    signal = "🔥 Strong Bullish"
                elif current > ma20:
                    signal = "📈 Bullish"
                elif rsi_val < 40:
                    signal = "⚠️ Strong Bearish"
                else:
                    signal = "📉 Bearish"

                results.append({
                    "Stock": stock,
                    "Price": current,
                    "SMA20": ma20,
                    "SMA50": ma50,
                    "RSI": rsi_val,
                    "Signal": signal
                })
            except Exception as e:
                st.warning(f"{stock}: {e}")

    if results:
        result_df = pd.DataFrame(results)
        st.dataframe(result_df, use_container_width=True)

        csv = result_df.to_csv(index=False)
        st.download_button(
            label="⬇️ Download CSV",
            data=csv,
            file_name="scanner_results.csv",
            mime="text/csv"
        )
    else:
        st.error("No stock data found.")

# ==================================================
# LOGOUT
# ==================================================
if st.sidebar.button("Logout"):
    st.session_state.clear()
    st.rerun()
