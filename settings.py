import streamlit as st


# =====================================
# SETTINGS PAGE
# =====================================
def show_settings():

    st.title("⚙️ Settings")

    # -----------------------------
    # User
    # -----------------------------
    st.subheader("👤 User")

    if "access_token" in st.session_state:
        st.success("✅ Logged in to FYERS")
    else:
        st.warning("❌ Not Logged In")

    st.divider()

    # -----------------------------
    # Auto Refresh
    # -----------------------------
    st.subheader("🔄 Auto Refresh")

    auto_refresh = st.toggle(
        "Enable Auto Refresh",
        value=st.session_state.get("auto_refresh", False)
    )

    refresh_interval = st.slider(
        "Refresh Interval (Seconds)",
        min_value=5,
        max_value=300,
        value=st.session_state.get("refresh_interval", 30),
        step=5
    )

    st.session_state["auto_refresh"] = auto_refresh
    st.session_state["refresh_interval"] = refresh_interval

    st.success("Settings Saved")

    st.divider()

    # -----------------------------
    # Theme Preference
    # -----------------------------
    st.subheader("🎨 Theme Preference")

    theme = st.selectbox(
        "Theme",
        [
            "System",
            "Light",
            "Dark"
        ],
        index=0
    )

    st.session_state["theme"] = theme

    st.info(
        "Theme preference is stored in the app. "
        "To actually change Streamlit appearance, "
        "use Settings → Appearance."
    )

    st.divider()

    # -----------------------------
    # Session
    # -----------------------------
    st.subheader("🧹 Session")

    if st.button("Clear Session"):

        for key in list(st.session_state.keys()):
            del st.session_state[key]

        st.success("Session Cleared")

        st.rerun()

    st.divider()

    # -----------------------------
    # Logout
    # -----------------------------
    st.subheader("🚪 Logout")

    if st.button("Logout"):

        if "access_token" in st.session_state:
            del st.session_state["access_token"]

        st.success("Logged Out")

        st.rerun()

    st.divider()

    # -----------------------------
    # About
    # -----------------------------
    st.subheader("ℹ️ About")

    st.info("""
FYERS Trading Dashboard

Version : 1.0

Modules Included

✅ Dashboard
✅ Market Watch
✅ Portfolio
✅ Orders
✅ Trading
✅ Option Chain
✅ AI Scanner
✅ Charts
✅ Profile
✅ Settings

Powered by FYERS API V3 + Streamlit
""")
