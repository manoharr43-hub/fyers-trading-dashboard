import streamlit as st
import pandas as pd

def show_market(fyers):
    st.title("📊 Live Market Analysis")
    
    symbol = st.text_input("Enter Symbol", "NSE:RELIANCE-EQ")
    col1, col2 = st.columns(2)

    # 1. GET QUOTE
    with col1:
        if st.button("📈 Get Quote"):
            try:
                response = fyers.quotes({"symbols": symbol})
                if response.get("s") == "ok":
                    data = response["d"][0]["v"]
                    st.success(f"LTP: {data.get('lp')}")
                    st.json(data)
                else:
                    st.error("Failed to fetch quote.")
            except Exception as e:
                st.error(f"Error: {e}")

    # 2. MARKET DEPTH (Fixed 'bids' Error)
    with col2:
        if st.button("📚 Market Depth"):
            try:
                depth_resp = fyers.depth({"symbol": symbol, "ohlcv_flag": "1"})
                
                if depth_resp.get("s") == "ok":
                    data = depth_resp.get("d", {})
                    
                    # 'bids' కీ ఉందో లేదో చెక్ చేయడం
                    if "bids" in data and len(data["bids"]) > 0:
                        bid_df = pd.DataFrame(data["bids"])
                        st.write("**Bids (Buyers)**")
                        st.dataframe(bid_df, use_container_width=True)
                    else:
                        st.warning("Market Depth data currently unavailable.")
                else:
                    st.error("Could not fetch market depth.")
            except Exception as e:
                st.error(f"Error: {e}")

    # 3. HISTORICAL DATA
    st.divider()
    st.subheader("📊 Historical Data")
    if st.button("Load History"):
        try:
            history = fyers.history({
                "symbol": symbol, "resolution": "D",
                "date_format": "1", "range_from": "2026-01-01",
                "range_to": "2026-12-31", "cont_flag": "1"
            })
            if "candles" in history:
                df = pd.DataFrame(history["candles"], columns=["Timestamp", "Open", "High", "Low", "Close", "Volume"])
                st.line_chart(df.set_index("Timestamp")["Close"])
            else:
                st.info("No historical data found.")
        except Exception as e:
            st.error(f"Error: {e}")
