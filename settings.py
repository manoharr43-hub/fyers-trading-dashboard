import streamlit as st

def show_settings():
    st.title("⚙️ Settings")

    # 1. User Status
    st.subheader("👤 User Account")
    if st.session_state.get("logged_in"):
        st.success("✅ Active Session: Logged in to FYERS")
    else:
        st.warning("❌ No Active Session")

    st.divider()

    # 2. Refresh & UI Settings
    st.subheader("🔄 App Preferences")
    
    col1, col2 = st.columns(2)
    with col1:
        auto_refresh = st.toggle("Auto Refresh", value=st.session_state.get("auto_refresh", False))
        st.session_state["auto_refresh"] = auto_refresh
    
    with col2:
        refresh_interval = st.slider("Refresh (sec)", 5, 300, st.session_state.get("refresh_interval", 30), step=5)
        st.session_state["refresh_interval"] = refresh_interval

    st.divider()

    # 3. Danger Zone (Logout/Reset)
    st.subheader("⚠️ Danger Zone")
    
    c1, c2 = st.columns(2)
    with c1:
        if st.button("🧹 Clear All Cache & Session"):
            st.session_state.clear()
            st.rerun()
            
    with c2:
        if st.button("🚪 Logout Now"):
            st.session_state["logged_in"] = False
            st.session_state.pop("access_token", None)
            st.rerun()

    st.divider()

    # 4. About Section
    with st.expander("ℹ️ About FYERS Dashboard"):
        st.write("""
        **Institutional Grade Trading Dashboard**
        - **Modules:** Real-time Market, Portfolio, AI Scanner, Option Chain.
        - **API:** FYERS API V3
        - **Version:** 1.0.0
        """)
