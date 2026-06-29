import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from fyers_apiv3 import fyersModel

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
            response = fyers.optionchain(data={"symbol": symbol, "strikecount": 20})
            
            # API Debugging: రెస్పాన్స్ ప్రింట్ అవుతుంది
            if response.get('s') == 'ok':
                # ఫైయర్స్ రెస్పాన్స్‌ని పరిశీలిస్తున్నాం
                data = response.get('data', {})
                
                # 'options' కీ ఉందో లేదో చూసి, ఉంటే ప్రాసెస్ చేస్తున్నాం
                if 'options' in data:
                    df = pd.DataFrame(data['options'])
                else:
                    # ఒకవేళ డేటా నేరుగా ఉంటే (కొన్నిసార్లు 'data' కీ లోనే ఉండొచ్చు)
                    df = pd.DataFrame(data) if isinstance(data, list) else pd.DataFrame([data])
                
                if not df.empty and 'strike_price' in df.columns:
                    # Key Metrics
                    col1, col2, col3 = st.columns(3)
                    pcr = df['pe_oi'].sum() / df['ce_oi'].sum() if df['ce_oi'].sum() > 0 else 0
                    col1.metric("Total CE OI", f"{df['ce_oi'].sum():,}")
                    col2.metric("Total PE OI", f"{df['pe_oi'].sum():,}")
                    col3.metric("PCR", round(pcr, 2))

                    # Table
                    st.dataframe(df[['strike_price', 'ce_ltp', 'ce_oi', 'pe_oi']].style.background_gradient(subset=['ce_oi', 'pe_oi'], cmap='Greens'), use_container_width=True)
                else:
                    st.warning("సింబల్ డేటా అభం లేదు.")
            else:
                st.error(f"API Error: {response.get('message')}")
        except Exception as e:
            st.error(f"System Error: {e}")


