import streamlit as st
import pandas as pd
import pandas_ta as ta
import plotly.graph_objects as go
from plotly.subplots import make_subplots

def show_charts(fyers):
    st.title("📊 Institutional Trading Engine")

    # 1. Sidebar Settings
    st.sidebar.header("⚙ Chart Settings")
    symbol = st.sidebar.text_input("Symbol", "NSE:RELIANCE-EQ")
    tf = st.sidebar.selectbox("Timeframe", ["1", "5", "15", "30", "60", "D"])
    
    # 2. Indicators Toggle
    st.sidebar.subheader("📈 Indicators")
    show_ema = st.sidebar.multiselect("EMA", [20, 50, 200], default=[20])
    show_rsi = st.sidebar.checkbox("RSI", False)
    show_macd = st.sidebar.checkbox("MACD", False)
    show_bb = st.sidebar.checkbox("Bollinger Bands", False)
    show_st = st.sidebar.checkbox("Supertrend", False)

    if st.button("🚀 Load Institutional Chart"):
        try:
            # Data Fetching (Assuming fyers history fetch logic)
            data = fyers.history({"symbol": symbol, "resolution": tf, "date_format": "0", "range_from": "2026-01-01", "range_to": "2026-12-31", "cont_flag": "1"})
            df = pd.DataFrame(data["candles"], columns=["Time", "Open", "High", "Low", "Close", "Volume"])
            df["Time"] = pd.to_datetime(df["Time"], unit="s")

            # 3. Technical Analysis Calculations using pandas_ta
            if show_ema:
                for length in show_ema:
                    df[f"EMA_{length}"] = ta.ema(df["Close"], length=length)
            if show_rsi: df["RSI"] = ta.rsi(df["Close"], length=14)
            if show_macd:
                macd = ta.macd(df["Close"])
                df = pd.concat([df, macd], axis=1)
            if show_bb:
                bb = ta.bbands(df["Close"], length=20)
                df = pd.concat([df, bb], axis=1)

            # 4. Plotting
            fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.7, 0.3], vertical_spacing=0.05)

            # Candlestick
            fig.add_trace(go.Candlestick(x=df["Time"], open=df["Open"], high=df["High"], low=df["Low"], close=df["Close"], name="Price"), row=1, col=1)

            # EMA Plots
            for length in show_ema:
                fig.add_trace(go.Scatter(x=df["Time"], y=df[f"EMA_{length}"], name=f"EMA {length}"), row=1, col=1)

            # Indicators (RSI/MACD)
            if show_rsi:
                fig.add_trace(go.Scatter(x=df["Time"], y=df["RSI"], name="RSI", line=dict(color='purple')), row=2, col=1)

            fig.update_layout(height=800, template="plotly_dark", xaxis_rangeslider_visible=False)
            st.plotly_chart(fig, use_container_width=True)

            # Export
            st.download_button("📥 Export CSV", df.to_csv(index=False), f"{symbol}_data.csv", "text/csv")
            
        except Exception as e:
            st.error(f"Error loading charts: {e}")
