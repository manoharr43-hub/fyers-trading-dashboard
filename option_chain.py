import streamlit as st
import pandas as pd

def show_option_chain(fyers):
    st.title("📊 NSE AI PRO V12 - Institutional Option Chain")

    # 1. Sidebar Settings
    index = st.sidebar.selectbox("Select Index", ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"])
    strike_count = st.sidebar.slider("Strike Count", 5, 30, 10)
    
    symbol_map = {
        "NIFTY": "NSE:NIFTY50-INDEX", "BANKNIFTY": "NSE:NIFTYBANK-INDEX",
        "FINNIFTY": "NSE:FINNIFTY-INDEX", "MIDCPNIFTY": "NSE:MIDCPNIFTY-INDEX"
    }

    # 2. Load Data
    if st.button("🔄 Load Institutional Option Chain"):
        with st.spinner("Fetching Data..."):
            try:
                res = fyers.optionchain({"symbol": symbol_map[index], "strikecount": strike_count})
                # డేటాను పద్ధతిగా తీసుకోవడం
                data = res.get("data", {}).get("optionsChain", [])
                df = pd.DataFrame(data)
                # 'oi' ని నంబర్‌గా మార్చడం
                if 'oi' in df.columns:
                    df['oi'] = pd.to_numeric(df['oi'], errors='coerce').fillna(0)
                st.session_state.oc_df = df
            except Exception as e: 
                st.error(f"Error fetching data: {e}")

    # 3. Analysis Dashboard
    if "oc_df" in st.session_state:
        df = st.session_state.oc_df
        
        # డేటాలో అవసరమైన కాలమ్స్ ఉన్నాయో లేదో చెక్ చేయడం
        required_cols = {'expiry', 'option_type', 'oi'}
        if required_cols.issubset(df.columns):
            # Expiry ఫిల్టర్
            unique_expiries = df['expiry'].unique()
            selected_expiry = st.sidebar.selectbox("Select Expiry Date", unique_expiries)
            df_filtered = df[df['expiry'] == selected_expiry]

            # CE మరియు PE ని వేరు చేయడం
            ce_df = df_filtered[df_filtered['option_type'] == 'CE']
            pe_df = df_filtered[df_filtered['option_type'] == 'PE']

            # Metrics
            total_ce_oi = ce_df['oi'].sum()
            total_pe_oi = pe_df['oi'].sum()
            pcr = total_pe_oi / total_ce_oi if total_ce_oi != 0 else 0

            # UI Metrics
            c1, c2, c3 = st.columns(3)
            c1.metric("PCR Ratio", round(pcr, 2))
            c2.metric("Total CE OI", int(total_ce_oi))
            c3.metric("Total PE OI", int(total_pe_oi))

            st.subheader(f"🔥 OI Analysis - {selected_expiry}")
            st.dataframe(df_filtered[['strike_price', 'option_type', 'oi', 'ltp', 'volume']], use_container_width=True)
        else:
            st.error("డేటాలో 'expiry', 'option_type', లేదా 'oi' కాలమ్స్ సరిగ్గా లేవు.")
            st.write("Available columns:", df.columns.tolist())

    st.caption("NSE AI PRO V12 | Institutional Edition")
