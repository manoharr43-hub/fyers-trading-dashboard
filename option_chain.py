import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from fyers_apiv3 import fyersModel

def show_option_chain(fyers):
    st.title("📊 Master Options Chain Dashboard")

    # 1. Selection
    option_type = st.sidebar.radio("Select Type", ["Indices", "F&O Stocks"])
    if option_type == "Indices":
        symbol = st.sidebar.selectbox("Select Index", 
            ["NSE:NIFTY-INDEX", "NSE:BANKNIFTY-INDEX", "NSE:FINNIFTY-INDEX", "NSE:MIDCPNIFTY-INDEX", "BSE:SENSEX-INDEX"])
    else:
        symbol = st.sidebar.text_input("Enter Symbol (e.g., NSE:RELIANCE-EQ)", "NSE:RELIANCE-EQ")

    if st.button("Fetch Live Data"):
        try:
            data = {"symbol": symbol, "strikecount": 20}
            response = fyers.optionchain(data=data)
            
            # ఇక్కడ మనం API రెస్పాన్స్ ఎలా ఉందో చెక్ చేస్తున్నాం
            if response and response.get('s') == 'ok':
                # 'data' కీ ఉందో లేదో చూస్తున్నాం
                if 'data' in response and 'options' in response['data']:
                    df = pd.DataFrame(response['data']['options'])
                    
                    if not df.empty:
                        # Key Metrics
                        col1, col2, col3 = st.columns(3)
                        pcr = df['pe_oi'].sum() / df['ce_oi'].sum() if df['ce_oi'].sum() != 0 else 0
                        col1.metric("Total CE OI", f"{df['ce_oi'].sum():,}")
                        col2.metric("Total PE OI", f"{df['pe_oi'].sum():,}")
                        col3.metric("PCR Ratio", round(pcr, 2))

                        # Table
                        st.subheader("Live Option Chain")
                        st.dataframe(df[['strike_price', 'ce_ltp', 'ce_oi', 'pe_oi', 'pe_ltp']].style.background_gradient(subset=['ce_oi', 'pe_oi'], cmap='Greens'), use_container_width=True)

                        # Chart
                        fig = go.Figure()
                        fig.add_trace(go.Bar(x=df['strike_price'], y=df['ce_oi'], name='CE OI', marker_color='red'))
                        fig.add_trace(go.Bar(x=df['strike_price'], y=df['pe_oi'], name='PE OI', marker_color='green'))
                        fig.update_layout(title='Open Interest Analysis', barmode='group')
                        st.plotly_chart(fig, use_container_width=True)
                    else:
                        st.warning("డేటా ఖాళీగా ఉంది.")
                else:
                    st.error("API రెస్పాన్స్‌లో 'options' అనే కీ లేదు. సింబల్ ఫార్మాట్ తప్పు కావచ్చు.")
                    st.write("API రెస్పాన్స్ డీటెయిల్స్:", response)
            else:
                st.error(f"API ఎర్రర్: {response.get('message', 'Unknown Error')}")
                
        except Exception as e:
            st.error(f"కోడ్ ఎర్రర్: {e}")
