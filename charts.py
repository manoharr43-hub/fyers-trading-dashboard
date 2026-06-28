import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

# టెక్నికల్ ఇండికేటర్ ఫంక్షన్స్
def get_ema(data, period):
    return data.ewm(span=period, adjust=False).mean()

def get_rsi(data, period=14):
    delta = data.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def show_charts(fyers):
    st.title("📊 Institutional Charting Engine")
    symbol = st.sidebar.text_input("Enter Symbol", "NSE:RELIANCE-EQ")
    
    if st.button("🚀 Load Chart"):
        try:
            # ఫ్యర్స్ నుండి డేటా పొందడం (ఉదాహరణ)
            history = fyers.history({
                "symbol": symbol, "resolution": "D",
                "date_format": "1", "range_from": "2026-01-01",
                "range_to": "2026-12-31", "cont_flag": "1"
            })
            
            df = pd.DataFrame(history["candles"], columns=["Timestamp", "Open", "High", "Low", "Close", "Volume"])
            
            # ఇండికేటర్స్ కాలిక్యులేషన్
            df['EMA_20'] = get_ema(df['Close'], 20)
            df['RSI'] = get_rsi(df['Close'], 14)
            
            # చార్ట్ గీయడం (Plotly)
            fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.1, row_heights=[0.7, 0.3])
            
            # Candlestick
            fig.add_trace(go.Candlestick(x=df['Timestamp'], open=df['Open'], high=df['High'], low=df['Low'], close=df['Close'], name='Price'), row=1, col=1)
            # EMA
            fig.add_trace(go.Scatter(x=df['Timestamp'], y=df['EMA_20'], name='EMA 20', line=dict(color='orange')), row=1, col=1)
            # RSI
            fig.add_trace(go.Scatter(x=df['Timestamp'], y=df['RSI'], name='RSI', line=dict(color='purple')), row=2, col=1)
            
            fig.update_layout(height=600, xaxis_rangeslider_visible=False)
            st.plotly_chart(fig, use_container_width=True)
            
        except Exception as e:
            st.error(f"Error: {e}")
