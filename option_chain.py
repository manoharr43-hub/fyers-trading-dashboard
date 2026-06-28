import streamlit as st
import pandas as pd
import time

def show_option_chain(fyers):
    st.title("📊 NSE AI PRO V12 - Institutional Option Chain")

    # --- Part 1: Settings ---
    symbol_map = {
        "NIFTY": "NSE:NIFTY50-INDEX", "BANKNIFTY": "NSE:NIFTYBANK-INDEX", 
        "FINNIFTY": "NSE:FINNIFTY-INDEX", "MIDCPNIFTY": "NSE:MIDCPNIFTY-INDEX", 
        "SENSEX": "BSE:SENSEX-INDEX", "BANKEX": "BSE:BANKEX-INDEX"
    }
    
    index = st.sidebar.selectbox("Select Index", list(symbol_map.keys()))
    strike_count = st.sidebar.slider("Strike Count", 5, 30, 10)

    # --- Part 2: Load Data & UI ---
    if st.button("🔄 Load Institutional Option Chain", use_container_width=True):
        with st.spinner("Fetching Data..."):
            try:
                res = fyers.optionchain({"symbol": symbol_map[index], "strikecount": strike_count})
                data = res.get("data", {}).get("optionsChain", [])
                st.session_state.oc_df = pd.DataFrame(data)
                st.rerun()
            except Exception as e:
                st.error(f"Error: {e}")

    if "oc_df" in st.session_state and not st.session_state.oc_df.empty:
        df = st.session_state.oc_df
        
        # Expiry Selection Box (టాప్‌లో)
        if 'expiry' in df.columns:
            selected_expiry = st.selectbox("📅 Select Expiry", df['expiry'].unique())
            df = df[df['expiry'] == selected_expiry].copy()
        
        # Calculations
        df['oi'] = pd.to_numeric(df['oi'], errors='coerce').fillna(0)
        ce_total = df[df['option_type'] == 'CE']['oi'].sum()
        pe_total = df[df['option_type'] == 'PE']['oi'].sum()

        # Top Metrics
        c1, c2 = st.columns(2)
        c1.metric("Total CE OI", f"{int(ce_total):,}")
        c2.metric("Total PE OI", f"{int(pe_total):,}")

        # Color Coding
        def highlight_rows(row):
            color = '#ffcccc' if row['option_type'] == 'CE' else '#ccffcc'
            return [f'background-color: {color}'] * len(row)

        st.dataframe(df.style.apply(highlight_rows, axis=1), use_container_width=True)

        # --- Part 3: OI Analysis ---
        st.divider()
        st.subheader("📊 Open Interest Analysis")
        # Logic to calculate PCR, Support, Resistance
        if ce_total > 0:
            pcr = pe_total / ce_total
            st.metric("PCR", round(pcr, 2))

        # --- Part 4, 5, 6: Advanced Institutional View ---
        st.divider()
        st.subheader("🏦 Institutional OI Analysis")
        # Add your Max Pain, Heatmap, and Export Logic here as before
        
        # --- Export ---
        csv = df.to_csv(index=False)
        st.download_button("⬇ Download Report", csv, f"{index}_Report.csv", "text/csv")
        
    else:
        st.info("👈 ఎడమ వైపు సెట్టింగ్స్ మార్చి, డేటాను లోడ్ చేయడానికి బటన్ క్లిక్ చేయండి.")

    st.sidebar.divider()
    st.sidebar.markdown("### 🚀 NSE AI PRO V12")
