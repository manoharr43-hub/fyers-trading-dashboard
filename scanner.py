import streamlit as st
import pandas as pd
import time

def show_scanner(fyers):
    st.title("🚀 NSE AI PRO V13 Institutional Scanner")
    
    # 1. Sidebar Filters
    st.sidebar.header("⚙ Scanner Filters")
    scanner_type = st.sidebar.selectbox("Scanner Type", ["AI Institutional", "Intraday", "Smart Money"])
    auto_refresh = st.sidebar.checkbox("Auto Refresh")
    refresh_rate = st.sidebar.slider("Refresh Seconds", 5, 60, 10)

    # 2. Main Dashboard
    c1, c2, c3, c4 = st.columns(4)
    scan_button = st.button("🚀 Run AI Scanner", use_container_width=True)

    if scan_button:
        try:
            symbols = ["NSE:RELIANCE-EQ", "NSE:TCS-EQ", "NSE:INFY-EQ", "NSE:HDFCBANK-EQ"]
            scanner_data = []

            for symbol in symbols:
                history = fyers.history({"symbol": symbol, "resolution": "D", "date_format": "1", "range_from": "2025-01-01", "range_to": "2026-12-31", "cont_flag": "1"})
                if history.get("s") != "ok": continue
                
                df = pd.DataFrame(history["candles"], columns=["Time", "Open", "High", "Low", "Close", "Volume"])
                
                # Logic: EMA, RVOL, AI Score
                close = df["Close"]
                ema20 = close.ewm(span=20, adjust=False).mean().iloc[-1]
                ema200 = close.ewm(span=200, adjust=False).mean().iloc[-1]
                rvol = df["Volume"].iloc[-1] / df["Volume"].tail(20).mean()
                
                scanner_data.append({
                    "Symbol": symbol, "Close": close.iloc[-1], "RVOL": round(rvol, 2),
                    "EMA20": ema20, "EMA200": ema200, "Smart Score": 75 # Mock Data
                })

            scan_df = pd.DataFrame(scanner_data)
            
            # 3. AI Engine & Ranking
            scan_df['AI Score'] = (scan_df['RVOL'] * 10) + (scan_df['Smart Score'] * 0.5)
            scan_df['Signal'] = scan_df['AI Score'].apply(lambda x: "🟢 BUY" if x > 60 else "🔴 SELL")
            
            st.subheader("🤖 Advanced AI Scanner")
            st.dataframe(scan_df, use_container_width=True)

            # 4. Smart Money Analysis
            st.subheader("💰 Smart Money Analysis")
            smart_data = []
            for _, row in scan_df.iterrows():
                smart_data.append({
                    "Symbol": row["Symbol"],
                    "AI Score": row["AI Score"],
                    "Smart Money Flow": "Institutional Accumulation" if row["AI Score"] > 70 else "Neutral",
                    "Signal": row["Signal"]
                })
            
            st.dataframe(pd.DataFrame(smart_data), use_container_width=True)
            st.success("✅ Analysis Complete.")

        except Exception as e:
            st.error(f"Error: {e}")

    if auto_refresh:
        time.sleep(refresh_rate)
        st.rerun()
