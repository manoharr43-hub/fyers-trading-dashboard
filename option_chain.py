import streamlit as st
import pandas as pd
import time

def show_option_chain(fyers):
    st.title("📊 NSE AI PRO V12 - Institutional Option Chain")

    # 1. Sidebar Settings
    st.sidebar.header("⚙️ Option Chain Settings")
    index = st.sidebar.selectbox("Select Index", ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"])
    strike_count = st.sidebar.slider("Strike Count", 5, 30, 10)
    
    symbol_map = {
        "NIFTY": "NSE:NIFTY50-INDEX", "BANKNIFTY": "NSE:NIFTYBANK-INDEX",
        "FINNIFTY": "NSE:FINNIFTY-INDEX", "MIDCPNIFTY": "NSE:MIDCPNIFTY-INDEX",
        "SENSEX": "BSE:SENSEX-INDEX", "BANKEX": "BSE:BANKEX-INDEX"
    }

    # 2. Spot Price Display
    try:
        quote = fyers.quotes({"symbols": symbol_map[index]})
        if quote.get("s") == "ok":
            q = quote["d"][0]["v"]
            c1, c2, c3 = st.columns(3)
            c1.metric("Spot", q.get("lp", 0), q.get("ch", 0))
            c2.metric("High", q.get("high_price", "-"))
            c3.metric("Low", q.get("low_price", "-"))
    except Exception as e:
        st.error(f"Error fetching spot: {e}")

    st.divider()

    # 3. Load Option Chain
    if st.button("🔄 Load Option Chain", use_container_width=True):
        with st.spinner("Fetching data..."):
            try:
                response = fyers.optionchain({"symbol": symbol_map[index], "strikecount": strike_count})
                if response.get("s") != "ok":
                    st.error("Failed to fetch data")
                    return
                
                data = response.get("data", {}).get("optionsChain", [])
                df = pd.DataFrame(data)
                st.session_state.oc_df = df # Save for analysis
                
                st.success("✅ Option Chain Loaded")
                st.dataframe(df, use_container_width=True)

            except Exception as e:
                st.error(f"Error: {e}")

    # 4. Institutional Analysis (Only if data exists)
    if "oc_df" in st.session_state:
        df = st.session_state.oc_df
        st.divider()
        st.subheader("📊 Institutional Analysis")
        
        # Simple Logic to identify Support/Resistance
        ce_oi = pd.to_numeric(df['ce_oi'], errors='coerce').fillna(0)
        pe_oi = pd.to_numeric(df['pe_oi'], errors='coerce').fillna(0)
        
        pcr = pe_oi.sum() / ce_oi.sum() if ce_oi.sum() != 0 else 0
        
        c1, c2, c3 = st.columns(3)
        c1.metric("PCR", round(pcr, 2))
        c2.success(f"Resistance: {df.loc[ce_oi.idxmax(), 'strike_price']}")
        c3.success(f"Support: {df.loc[pe_oi.idxmax(), 'strike_price']}")

        # AI Signal
        st.subheader("🤖 AI Institutional Signal")
        if pcr > 1.3:
            st.success("🟢 Bullish: Strong Put Writing")
        elif pcr < 0.7:
            st.error("🔴 Bearish: Heavy Call Writing")
        else:
            st.info("🟡 Neutral Market")
