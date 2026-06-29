import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from fyers_apiv3 import fyersModel

def show_option_chain(fyers):
    st.title("📊 Master Options Chain Dashboard")

    # 1. Symbol Mapping based on your Markets
    symbol_map = {
        "NIFTY 50": "NSE:NIFTY50-INDEX",
        "NIFTY BANK": "NSE:NIFTYBANK-INDEX",
        "SENSEX": "BSE:SENSEX-INDEX",
        "BANKEX": "BSE:BANKEX-INDEX",
        "NIFTY NXT 50": "NSE:NIFTYNEXT50-INDEX"
    }

    option_type = st.sidebar.radio("Select Type", ["Indices", "F&O Stocks"])
    
    if option_type == "Indices":
        selected_key = st.sidebar.selectbox("Select Index", list(symbol_map.keys()))
        symbol = symbol_map[selected_key]
    else:
        stock_symbol = st.sidebar.text_input("Enter Stock (e.g., RELIANCE)", "RELIANCE")
        symbol = f"NSE:{stock_symbol.upper()}-EQ"

    # 2. Fetch Data
    if st.button("Fetch Live Data"):
        try:
            # API Request
            data = {"symbol": symbol, "strikecount": 20}
            response = fyers.optionchain(data=data)
            
            if response and response.get('s') == 'ok' and 'data' in response:
                df = pd.DataFrame(response['data']['options'])
                
                if not df.empty:
                    # Metrics
                    col1, col2, col3 = st.columns(3)
                    pcr = df['pe_oi'].sum() / df['ce_oi'].sum() if df['ce_oi'].sum() != 0 else 0
                    col1.metric("Total CE OI", f"{df['ce_oi'].sum():,}")
                    col2.metric("Total PE OI", f"{df['pe_oi'].sum():,}")
                    col3.metric("PCR Ratio", round(pcr, 2))

                    # Table
                    st.subheader(f"Live Option Chain: {selected_key if option_type == 'Indices' else stock_symbol}")
                    st.dataframe(df[['strike_price', 'ce_ltp', 'ce_oi', 'pe_oi', 'pe_ltp']].style.background_gradient(subset=['ce_oi', 'pe_oi'], cmap='Greens'), use_container_width=True)

                    # Chart
                    fig = go.Figure()
                    fig.add_trace(go.Bar(x=df['strike_price'], y=df['ce_oi'], name='CE OI (Resistance)', marker_color='red'))
                    fig.add_trace(go.Bar(x=df['strike_price'], y=df['pe_oi'], name='PE OI (Support)', marker_color='green'))
                    fig.update_layout(title='Open Interest Analysis', barmode='group')
                    st.plotly_chart(fig, use_container_width=True)
                else:
                    st.warning("ఈ సింబల్ కోసం డేటా అందుబాటులో లేదు.")
            else:
                st.error(f"API ఎర్రర్: {response.get('message', 'Invalid Symbol')}")
                
        except Exception as e:
            st.error(f"సాంకేతిక లోపం: {e}")

# 
