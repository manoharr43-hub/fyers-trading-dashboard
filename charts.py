import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import time

def show_charts(fyers):
    st.title("📈 Advanced Trading Charts")

    # Sidebar
    st.sidebar.header("⚙ Chart Settings")
    symbol = st.sidebar.text_input("Symbol", "NSE:RELIANCE-EQ")
    resolution = st.sidebar.selectbox("Timeframe", ["1", "5", "15", "30", "60", "D"])
    days = st.sidebar.slider("History (Days)", 5, 365, 30)

    if st.button("📊 Load Chart", use_container_width=True):
        try:
            # FYERS API call
            data = fyers.history({
                "symbol": symbol,
                "resolution": resolution,
                "date_format": "0",
                "range_from": "2026-01-01", # ఇక్కడ తేదీ ఫార్మాట్ సరిచూసుకోండి
                "range_to": "2026-12-31",
                "cont_flag": "1"
            })

            if data.get("s") != "ok":
                st.error("Data Load Error")
                return

            candles = data["candles"]
            df = pd.DataFrame(candles, columns=["Time", "Open", "High", "Low", "Close", "Volume"])
            df["Time"] = pd.to_datetime(df["Time"], unit="s")

            # Plotly CandleStick Chart
            fig = make_subplots(rows=2, cols=1, shared_xaxes=True, 
                                vertical_spacing=0.03, subplot_titles=(symbol, 'Volume'), 
                                row_width=[0.2, 0.7])

            fig.add_trace(go.Candlestick(x=df['Time'], open=df['Open'], high=df['High'],
                                         low=df['Low'], close=df['Close'], name='Price'), row=1, col=1)

            fig.add_trace(go.Bar(x=df['Time'], y=df['Volume'], name='Volume'), row=2, col=1)

            fig.update_layout(xaxis_rangeslider_visible=False, height=600)
            
            st.plotly_chart(fig, use_container_width=True)

        except Exception as e:
            st.error(f"Error: {e}")

    # Auto Refresh Logic
    if st.sidebar.checkbox("Auto Refresh"):
        time.sleep(10)
        st.rerun()
