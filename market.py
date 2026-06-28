import streamlit as st
import pandas as pd

def show_market(fyers):
    st.title("📊 Live Market Analysis")

    symbol = st.text_input("Enter Symbol", "NSE:RELIANCE-EQ")
    col1, col2 = st.columns(2)

    # 1. LIVE QUOTE
    with col1:
        if st.button("📈 Get Quote", use_container_width=True):
            try:
                quote = fyers.quotes({"symbols": symbol})
                if quote.get("s") == "ok":
                    data = quote["d"][0]["v"]
                    st.metric("Last Price", f"₹{data['lp']}", f"{data['ch']}%")
                    st.write(f"**High:** {data['high_price']} | **Low:** {data['low_price']}")
            except Exception as e:
                st.error(f"Error: {e}")

    # 2. MARKET DEPTH (BID/ASK Table)
    with col2:
        if st.button("📚 Market Depth", use_container_width=True):
            try:
                depth = fyers.depth({"symbol": symbol, "ohlcv_flag": "1"})
                if depth.get("s") == "ok":
                    # బిడ్ మరియు ఆస్క్ డేటాను టేబుల్ రూపంలో చూపడం
                    bid_df = pd.DataFrame(depth["d"]["bids"])
                    ask_df = pd.DataFrame(depth["d"]["asks"])
                    st.write("**Bids (Buyers)**")
                    st.dataframe(bid_df, use_container_width=True)
            except Exception as e:
                st.error(f"Error: {e}")

    st.divider()

    # 3. HISTORICAL DATA (Formatted)
    st.subheader("📉 Historical Data")
    if st.button("Load History"):
        try:
            history = fyers.history({
                "symbol": symbol, "resolution": "5", "date_format": "1",
                "range_from": "2026-06-01", "range_to": "2026-06-28", "cont_flag": "1"
            })
            if history.get("candles"):
                df = pd.DataFrame(history["candles"], columns=["Timestamp", "Open", "High", "Low", "Close", "Volume"])
                df["Timestamp"] = pd.to_datetime(df["Timestamp"], unit="s")
                st.dataframe(df.sort_values("Timestamp", ascending=False), use_container_width=True)
            else:
                st.warning("No Data Found")
        except Exception as e:
            st.error(f"Error: {e}")
