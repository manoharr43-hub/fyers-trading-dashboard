import streamlit as st
import pandas as pd
from fyers_apiv3 import fyersModel

# PAGE CONFIG
st.set_page_config(page_title="NSE AI PRO V11.11", layout="wide")

# LOGIN & SESSION
CLIENT_ID = st.secrets["FYERS_CLIENT_ID"]
SECRET_KEY = st.secrets["FYERS_SECRET_KEY"]
REDIRECT_URI = st.secrets["FYERS_REDIRECT_URI"]

if "access_token" not in st.session_state: st.session_state["access_token"] = None

def get_fyers():
    return fyersModel.FyersModel(client_id=CLIENT_ID, token=st.session_state["access_token"], is_async=False, log_path="")

# NAVIGATION
menu = st.sidebar.radio("Navigation", ["Dashboard", "AI Scanner", "Trade Console"])

# AI SCANNER LOGIC
if menu == "AI Scanner":
    st.title("🚀 NSE AI PRO V11.11 - Big Move Hunter")
    
    # Segment selection
    seg = st.sidebar.selectbox("Select Segment", ["Stocks", "Index", "F&O", "Futures"])
    
    if seg == "Index":
        watch_list = ["NSE:NIFTY50-INDEX", "NSE:NIFTYBANK-INDEX", "NSE:NIFTYMID50-INDEX", "NSE:FINNIFTY-INDEX"]
    elif seg == "Stocks":
        watch_list = ["NSE:SBIN-EQ", "NSE:RELIANCE-EQ", "NSE:TCS-EQ"]
    else:
        watch_list = ["NSE:NIFTY26JUN2026FUT", "NSE:BANKNIFTY26JUN2026FUT"]

    if st.button("RUN AI SCANNER"):
        fyers = get_fyers()
        report = []
        
        for sym in watch_list:
            res = fyers.history(data={"symbol": sym, "resolution": "15", "date_format": "1", "range_from": "2026-06-24", "range_to": "2026-06-25", "cont_flag": "1"})
            
            if res['s'] == 'ok':
                df = pd.DataFrame(res['candles'], columns=['ts', 'o', 'h', 'l', 'c', 'v'])
                
                # Logic
                range_size = df['h'].max() - df['l'].min()
                avg_vol = df['v'].rolling(20).mean().iloc[-1]
                volume_burst = df['v'].iloc[-1] > (avg_vol * 1.5)
                
                if range_size > (df['c'].iloc[-1] * 0.008) and volume_burst:
                    status = "🚀 STRONG BIG MOVE"
                elif range_size > (df['c'].iloc[-1] * 0.005):
                    status = "📈 MOMENTUM"
                else:
                    status = "➖ SIDEWAYS"
                
                report.append({"Symbol": sym, "LTP": df['c'].iloc[-1], "Status": status})
        
        st.session_state.v11_master_data = pd.DataFrame(report)
        st.success("✅ Scanning Complete!")

    if not st.session_state.get("v11_master_data") is None:
        st.dataframe(st.session_state.v11_master_data, use_container_width=True)
        # Alert Box
        big_moves = st.session_state.v11_master_data[st.session_state.v11_master_data['Status'] == "🚀 STRONG BIG MOVE"]
        if not big_moves.empty:
            st.error("🚨 ALERT: BIG MOVES IDENTIFIED")
            st.table(big_moves)

# TRADE CONSOLE
elif menu == "Trade Console":
    st.title("🛒 Trade Console")
    symbol = st.text_input("Symbol", "NSE:SBIN-EQ")
    qty = st.number_input("Quantity", value=1)
    if st.button("Buy"):
        st.write(get_fyers().place_order(data={"symbol": symbol, "qty": qty, "type": 1, "side": 1, "productType": "INTRADAY", "validity": "DAY"}))
    if st.button("Sell"):
        st.write(get_fyers().place_order(data={"symbol": symbol, "qty": qty, "type": 1, "side": -1, "productType": "INTRADAY", "validity": "DAY"}))

# DASHBOARD
else:
    st.title("📈 Dashboard")
    if st.session_state["access_token"]:
        fyers = get_fyers()
        st.subheader("Positions")
        st.json(fyers.positions())
    else:
        st.write("Please login to see data.")
