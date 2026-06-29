import streamlit as st
import pandas as pd
import plotly.graph_objects as go

def show_option_chain(fyers):
    st.title("📊 Master Options Chain Dashboard")

    # 1. Symbol Mapping
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
        stock = st.sidebar.text_input("Enter Stock (e.g., RELIANCE)", "RELIANCE")
        symbol = f"NSE:{stock.upper()}-EQ"

    if st.button("Fetch Live Data"):
        try:
            # API కాల్
            response = fyers.optionchain(data={"symbol": symbol, "strikecount": 20})
            
            # డేటా చెకింగ్
            if response and response.get('s') == 'ok':
                data = response.get('data', {})
                options_data = data.get('options', [])
                
                if options_data:
                    df = pd.DataFrame(options_data)
                    
                    # Metrics
                    col1, col2, col3 = st.columns(3)
                    pcr = df['pe_oi'].sum() / df['ce_oi'].sum() if df['ce_oi'].sum() > 0 else 0
                    col1.metric("Total CE OI", f"{df['ce_oi'].sum():,}")
                    col2.metric("Total PE OI", f"{df['pe_oi'].sum():,}")
                    col3.metric("PCR", round(pcr, 2))

                    # Table
                    st.dataframe(df[['strike_price', 'ce_ltp', 'ce_oi', 'pe_oi', 'pe_ltp']].style.background_gradient(subset=['ce_oi', 'pe_oi'], cmap='Greens'), use_container_width=True)
                else:
                    st.warning("మార్కెట్ క్లోజ్ అయింది లేదా ప్రస్తుతం ఈ సింబల్ డేటా అందుబాటులో లేదు.")
            else:
                st.error(f"API Error: {response.get('message', 'Data not found')}")
                
        except Exception as e:
            st.error(f"System Error: {e}")

# 
